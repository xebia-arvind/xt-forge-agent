import time
import uuid
import logging
import os
import re
from datetime import timedelta
from typing import Dict, Any, Optional, List

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated

from curertestai.serializers import (
    HealRequestSerializer,
    BatchHealRequestSerializer,
    HealResponseSerializer,
    BatchHealResponseSerializer
)
from curertestai.models import HealerRequest, SuggestedSelector
from curertestai.dom_extractor import DOMExtractor
from curertestai.matching_engine import MatchingEngine
from curertestai.validation_engine import select_validated_candidate
from curertestai.fingerprint import (
    build_dom_signature_tokens,
    generate_dom_fingerprint,
    jaccard_similarity,
)
from ui_knowledge.change_detection_service import detect_ui_change_for_healing
from clients.models import Clients
from django.contrib.auth import get_user_model
from django.utils import timezone
User = get_user_model()

# Setup logger
logger = logging.getLogger(__name__)


class HealAPIView(APIView):
    """
    POST /heal/
    Heal a single failing selector
    """
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        """Handle single heal request"""
        start_time = time.time()
        request_id = str(uuid.uuid4())
        user = request.user
        client_secret = request.auth.get('client_id')
        try:
            client = Clients.objects.get(secret_key=client_secret)
        except Clients.DoesNotExist:
            # Handle error scenario if needed
            raise ValueError("Invalid Client ID in token")
        # Validate incoming data
        
        serializer = HealRequestSerializer(data=request.data)
        if not serializer.is_valid():
            logger.error(f"Validation failed | request_id={request_id} | errors={serializer.errors}")
            return Response(
                {"error": "Validation failed", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        validated_data = serializer.validated_data
        
        try:
            cached_result = self._resolve_cached_selector(validated_data, start_time)
            if cached_result:
                result = cached_result
            else:
                # Process heal request
                result = self._process_heal_request(
                    validated_data,
                    request_id,
                    start_time,
                    client=client,
                )
            
            # Save to database
            healer_request = self._save_heal_request(validated_data, result, user=user, client=client)

            # Inject IDs into response. batch_id retained for client back-compat (always 0 for single heals).
            result['id'] = healer_request.id
            result['batch_id'] = 0
            
            logger.info(
                f"Heal successful | request_id={request_id} | "
                f"selector={validated_data.get('failed_selector')} | "
                f"chosen={result.get('chosen')}"
            )
            
            return Response(result, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(
                f"Healing failed | request_id={request_id} | "
                f"selector={validated_data.get('failed_selector')} | "
                f"error={str(e)}",
                exc_info=True
            )
            # Fail-safe response so test layer can handle it as NO_SAFE_MATCH
            # instead of hard failing on HTTP 500.
            safe_response = {
                "id": 0,
                "batch_id": 0,
                "message": "Healer internal failure handled safely",
                "chosen": None,
                "validation_status": "NO_SAFE_MATCH",
                "validation_reason": f"Healer internal error: {str(e)[:200]}",
                "llm_used": False,
                "history_assisted": False,
                "history_hits": 0,
                "retrieval_assisted": False,
                "retrieval_hits": 0,
                "retrieved_versions": [],
                "dom_fingerprint": None,
                "ui_change_level": "UNKNOWN",
                "candidates": [],
                "debug": {
                    "total_candidates": 0,
                    "engine": "safe_fallback",
                    "processing_time_ms": round((time.time() - start_time) * 1000, 2),
                    "vision_analyzed": False,
                    "validation_status": "NO_SAFE_MATCH",
                    "validation_reason": "Healer internal exception",
                    "history_assisted": False,
                    "history_hits": 0,
                    "retrieval_assisted": False,
                    "retrieval_hits": 0,
                    "retrieved_versions": [],
                    "error": str(e),
                },
            }
            return Response(safe_response, status=status.HTTP_200_OK)

    def _resolve_cached_selector(
        self,
        validated_data: Dict[str, Any],
        start_time: float,
    ) -> Optional[Dict[str, Any]]:
        """
        Fast-path: reuse a previously validated successful selector for same failure context.
        Falls back to full healing pipeline if no safe cache hit exists.
        """
        use_cache = os.getenv("USE_HEALING_CACHE", "true").lower() == "true"
        skip_cache = bool(validated_data.get("skip_cache", False))
        if not use_cache or skip_cache:
            return None

        page_url = validated_data.get("page_url", "") or ""
        use_of_selector = validated_data.get("use_of_selector", "") or ""
        failed_selector = validated_data.get("failed_selector", "") or ""
        intent_key = validated_data.get("intent_key", "") or ""

        if not (page_url and use_of_selector and failed_selector):
            return None

        min_confidence = float(os.getenv("HEALING_CACHE_MIN_CONFIDENCE", "0.30"))
        max_age_days = int(os.getenv("HEALING_CACHE_MAX_AGE_DAYS", "14"))
        cutoff = timezone.now() - timedelta(days=max_age_days)

        cache_qs = (
            HealerRequest.objects.filter(
                url=page_url,
                use_of_selector=use_of_selector,
                failed_selector=failed_selector,
                success=True,
                validation_status="VALID",
                created_on__gte=cutoff,
                confidence__gte=min_confidence,
            )
            .exclude(healed_selector__isnull=True)
            .exclude(healed_selector__exact="")
        )

        if intent_key:
            cache_qs = cache_qs.filter(intent_key=intent_key)

        cached = cache_qs.order_by("-created_on").first()
        if not cached:
            return None

        processing_time = round((time.time() - start_time) * 1000, 2)
        chosen = cached.healed_selector
        confidence = float(cached.confidence or 0.0)

        logger.info(
            "Cache hit for healer request | failed_selector=%s | chosen=%s | source_id=%s",
            failed_selector,
            chosen,
            cached.id,
        )

        return {
            "message": "Resolved from history cache",
            "chosen": chosen,
            "validation_status": "VALID",
            "validation_reason": "Reused previously successful validated selector",
            "llm_used": False,
            "history_assisted": True,
            "history_hits": 1,
            "dom_fingerprint": cached.dom_fingerprint,
            "ui_change_level": cached.ui_change_level or "UNKNOWN",
            "candidates": [
                {
                    "selector": chosen,
                    "score": round(confidence, 4),
                    "base_score": round(confidence, 4),
                    "attribute_score": 0.0,
                    "tag": "",
                    "text": "",
                    "xpath": "",
                }
            ],
            "debug": {
                "total_candidates": 1,
                "engine": "history_cache",
                "processing_time_ms": processing_time,
                "vision_analyzed": False,
                "validation_status": "VALID",
                "validation_reason": "Cache hit",
                "history_assisted": True,
                "history_hits": 1,
                "dom_fingerprint": cached.dom_fingerprint,
                "ui_change_level": cached.ui_change_level or "UNKNOWN",
                "cache_hit": True,
                "cache_source_id": cached.id,
            },
        }
    
    def _process_heal_request(
        self,
        validated_data: Dict[str, Any],
        request_id: str,
        start_time: float,
        client=None,
    ) -> Dict[str, Any]:
        """Process a single heal request and return response"""

        # Prepare DOM data for processing
        html_content = validated_data.get('html', '')

        if validated_data.get('semantic_dom') and isinstance(validated_data.get('semantic_dom'), dict):
            # Use provided semantic DOM
            dom_data = validated_data['semantic_dom']
        elif html_content:
            # Fallback to extraction if not provided but HTML is
            extractor = DOMExtractor(html_content)
            dom_data = extractor.extract_semantic_dom(full_coverage=True)
        else:
            raise ValueError("No DOM source provided")

        current_elements = dom_data.get("elements", [])
        dom_fingerprint = generate_dom_fingerprint(current_elements)
        current_signature_tokens = sorted(build_dom_signature_tokens(current_elements))[:500]
        ui_change_level = self._detect_ui_change_level(validated_data, current_elements, client=client)

        # Initialize matching engine and rank candidates.
        # If ranking fails, continue with empty candidates so validation
        # returns NO_SAFE_MATCH instead of raising 500.
        engine_error = None
        try:
            engine = MatchingEngine(current_elements)
            results = engine.rank(
                validated_data['failed_selector'],
                validated_data['use_of_selector'],
                top_k=5
            )
        except Exception as exc:
            logger.warning(
                "Matching engine failed | request_id=%s | selector=%s | error=%s",
                request_id,
                validated_data.get("failed_selector"),
                str(exc),
            )
            engine_error = str(exc)
            results = []
        
        # Calculate processing time
        processing_time = (time.time() - start_time) * 1000
        
        # Build response
        response = self._build_heal_response(
            engine_results=results,
            failed_selector=validated_data["failed_selector"],
            use_of_selector=validated_data["use_of_selector"],
            page_url=validated_data.get("page_url", ""),
            intent_key=validated_data.get("intent_key", ""),
            dom_fingerprint=dom_fingerprint,
            current_signature_tokens=current_signature_tokens,
            ui_change_level=ui_change_level,
            request_id=request_id,
            processing_time=processing_time,
            engine_error=engine_error,
        )
        
        return response
    
    def _build_heal_response(
        self,
        engine_results: list,
        failed_selector: str,
        use_of_selector: str,
        page_url: str,
        intent_key: str,
        dom_fingerprint: str,
        current_signature_tokens: List[str],
        ui_change_level: str,
        request_id: str,
        processing_time: float,
        engine_error: Optional[str] = None,
        vision_analyzed: bool = False,
        vision_analysis: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Build heal response from engine results"""
        
        candidates = []
        
        for r in engine_results:
            el = r["element"]
            candidates.append({
                "selector": r["suggested"],
                "score": round(float(r["score"]), 4),
                "base_score": round(float(r["base"]), 4),
                "attribute_score": round(float(r["attr"]), 4),
                "tag": el.get("tag"),
                "text": el.get("text") or el.get("accessible_name"),
                "xpath": el.get("xpath"),
            })
        
        debug_info = {
            "total_candidates": len(candidates),
            "engine": "matching_engine_faiss",
            "processing_time_ms": round(processing_time, 2),
            "vision_analyzed": vision_analyzed,
        }
        
        if vision_analyzed and vision_analysis:
            debug_info["vision_model"] = vision_analysis.get("model_used")
            debug_info["vision_success"] = vision_analysis.get("success", False)
        if engine_error:
            debug_info["engine_error"] = engine_error

        selection = select_validated_candidate(
            candidates=candidates,
            failed_selector=failed_selector,
            use_of_selector=use_of_selector,
            page_url=page_url,
            intent_key=intent_key,
            current_signature_tokens=current_signature_tokens,
            ui_change_level=ui_change_level,
        )
        debug_info["validation_status"] = selection["validation_status"]
        debug_info["validation_reason"] = selection["validation_reason"]
        debug_info["history_assisted"] = selection["history_assisted"]
        debug_info["history_hits"] = selection["history_hits"]
        debug_info["retrieval_assisted"] = selection.get("retrieval_assisted", False)
        debug_info["retrieval_hits"] = selection.get("retrieval_hits", 0)
        debug_info["retrieved_versions"] = selection.get("retrieved_versions", [])
        debug_info["dom_fingerprint"] = dom_fingerprint
        debug_info["ui_change_level"] = ui_change_level

        return {
            "message": "Success",
            "chosen": selection["chosen"],
            "validation_status": selection["validation_status"],
            "validation_reason": selection["validation_reason"],
            "llm_used": selection["llm_used"],
            "history_assisted": selection["history_assisted"],
            "history_hits": selection["history_hits"],
            "retrieval_assisted": selection.get("retrieval_assisted", False),
            "retrieval_hits": selection.get("retrieval_hits", 0),
            "retrieved_versions": selection.get("retrieved_versions", []),
            "signature_tokens": current_signature_tokens,
            "dom_fingerprint": dom_fingerprint,
            "ui_change_level": ui_change_level,
            "candidates": candidates,
            "debug": debug_info
        }
    
    def _save_heal_request(
        self,
        validated_data: Dict[str, Any],
        result: Dict[str, Any],
        user: Optional[User] = None,
        client: Optional[Clients] = None,
    ):
        """Save heal request and its candidate selectors."""

        processing_time_ms = int(result['debug']['processing_time_ms'])
        chosen_selector = result.get('chosen', '')
        candidates = result.get('candidates', [])

        healer_request = HealerRequest.objects.create(
            user_id=user,
            client_id=client,
            failed_selector=validated_data['failed_selector'],
            html=validated_data.get('html', ''),
            use_of_selector=validated_data['use_of_selector'],
            selector_type=validated_data.get('selector_type', 'css'),
            url=validated_data.get('page_url', ''),
            healed_selector=chosen_selector or '',
            confidence=candidates[0]['score'] if candidates else 0.0,
            success=bool(chosen_selector),
            processing_time_ms=processing_time_ms,
            llm_used=bool(result.get("llm_used", False)),
            screenshot_analyzed=False,
            intent_key=validated_data.get("intent_key", ""),
            validation_status=result.get("validation_status"),
            validation_reason=result.get("validation_reason"),
            dom_fingerprint=result.get("dom_fingerprint"),
            candidate_snapshot=candidates[:5] if candidates else [],
            history_assisted=bool(result.get("history_assisted", False)),
            history_hits=int(result.get("history_hits", 0) or 0),
            ui_change_level=result.get("ui_change_level"),
        )

        for candidate in candidates[:5]:
            SuggestedSelector.objects.create(
                healer_request=healer_request,
                selector=candidate['selector'],
                score=candidate['score'],
                base_score=candidate['base_score'],
                attribute_score=candidate['attribute_score'],
                tag=candidate.get('tag', ''),
                text=candidate.get('text', ''),
                xpath=candidate.get('xpath', ''),
            )

        return healer_request

    def _detect_ui_change_level(
        self,
        validated_data: Dict[str, Any],
        current_elements: List[Dict[str, Any]],
        client=None,
    ) -> str:
        page_url = validated_data.get("page_url", "") or ""
        use_of_selector = validated_data.get("use_of_selector", "") or ""
        intent_key = validated_data.get("intent_key", "") or ""
        failed_selector = validated_data.get("failed_selector", "") or ""

        # Primary signal: ui_knowledge baseline/current diff service.
        try:
            ui_change = detect_ui_change_for_healing(
                page_url=page_url,
                failed_selector=failed_selector,
                use_of_selector=use_of_selector,
                client=client,
            )
            level = str(ui_change.get("ui_change_level") or "UNKNOWN").upper()
            if level and level != "UNKNOWN":
                return level
        except Exception as exc:
            logger.debug("ui_knowledge change detection fallback to healer heuristic: %s", str(exc))

        base_query = HealerRequest.objects.filter(
            url=page_url,
            use_of_selector=use_of_selector,
            success=True,
        ).exclude(html__isnull=True).exclude(html__exact="")

        previous = None
        if intent_key:
            previous = base_query.filter(intent_key=intent_key).order_by("-created_on").first()
        if not previous:
            previous = base_query.order_by("-created_on").first()

        hints = self._extract_selector_hints(failed_selector, use_of_selector)
        if not previous:
            # If selector hints are clearly missing in current DOM, don't keep UNKNOWN.
            if hints and not self._selector_hint_exists(current_elements, hints):
                return "MINOR_CHANGE"
            return "UNKNOWN"

        try:
            extractor = DOMExtractor(previous.html)
            previous_dom = extractor.extract_semantic_dom(full_coverage=True)
            previous_elements = previous_dom.get("elements", [])
        except Exception:
            return "UNKNOWN"

        if hints:
            had_before = self._selector_hint_exists(previous_elements, hints)
            exists_now = self._selector_hint_exists(current_elements, hints)
            if had_before and not exists_now:
                return "ELEMENT_REMOVED"

        prev_tokens = build_dom_signature_tokens(previous_elements)
        curr_tokens = build_dom_signature_tokens(current_elements)
        similarity = jaccard_similarity(prev_tokens, curr_tokens)

        if similarity >= 0.90:
            return "UNCHANGED"
        if similarity >= 0.70:
            return "MINOR_CHANGE"

        return "MAJOR_CHANGE"

    def _extract_selector_hints(self, failed_selector: str, use_of_selector: str) -> List[str]:
        """
        Build lightweight hints from selector + step intent text.
        Example:
          a:has-text("View Details") -> ["view details"]
          #buy-btn -> ["buy-btn"]
          [data-testid="cart-icon"] -> ["cart-icon"]
        """
        hints: List[str] = []
        sel = (failed_selector or "").strip().lower()
        use_text = (use_of_selector or "").strip().lower()

        text_match = re.search(r'has-text\\("([^"]+)"\\)', sel)
        if text_match:
            hints.append(text_match.group(1).strip())
        text_match_single = re.search(r"has-text\\('([^']+)'\\)", sel)
        if text_match_single:
            hints.append(text_match_single.group(1).strip())

        id_match = re.search(r"#([a-zA-Z0-9_-]+)", sel)
        if id_match:
            hints.append(id_match.group(1).strip().lower())

        testid_match = re.search(r'data-testid\\s*=\\s*["\\\']([^"\\\']+)["\\\']', sel)
        if testid_match:
            hints.append(testid_match.group(1).strip().lower())

        attr_literal_matches = re.findall(
            r'(?:id|name|role|aria-label|data-testid)\\s*=\\s*["\\\']([^"\\\']+)["\\\']',
            sel
        )
        for val in attr_literal_matches[:3]:
            hints.append(val.strip().lower())

        class_matches = re.findall(r"\\.([a-zA-Z0-9_-]+)", sel)
        for klass in class_matches[:2]:
            hints.append(klass.strip().lower())

        # Generic fallback: use meaningful words from step intent text.
        generic_words = [
            token for token in re.split(r"[^a-z0-9]+", use_text)
            if token and len(token) >= 4 and token not in {"click", "button", "first", "with", "self", "healing"}
        ]
        hints.extend(generic_words[:4])

        # De-duplicate and keep meaningful hints only.
        normalized = []
        seen = set()
        for h in hints:
            val = " ".join(h.split()).strip().lower()
            if len(val) < 3 or val in seen:
                continue
            normalized.append(val)
            seen.add(val)
        return normalized[:5]

    def _selector_hint_exists(self, elements: List[Dict[str, Any]], hints: List[str]) -> bool:
        if not hints:
            return False

        for el in elements[:1000]:
            attrs = el.get("attributes") or {}
            blob_parts = [
                str(el.get("text") or ""),
                str(el.get("accessible_name") or ""),
                str(el.get("selector") or ""),
                str(attrs.get("id") or ""),
                str(attrs.get("class") or ""),
                str(attrs.get("data-testid") or ""),
                str(attrs.get("aria-label") or ""),
                str(attrs.get("name") or ""),
            ]
            blob = " ".join(blob_parts).lower()
            if any(h in blob for h in hints):
                return True
        return False


class BatchHealAPIView(APIView):
    """
    POST /heal/batch/
    Heal multiple failing selectors in a batch
    """
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        """Handle batch heal request"""
        batch_start_time = time.time()
        request_id = str(uuid.uuid4())
        user = request.user
        client_secret = request.auth.get('client_id')
        try:
            client = Clients.objects.get(secret_key=client_secret)
        except Clients.DoesNotExist:
            # Handle error scenario if needed
            raise ValueError("Invalid Client ID in token")
        
        # Validate incoming data
        serializer = BatchHealRequestSerializer(data=request.data)
        if not serializer.is_valid():
            logger.error(f"Batch validation failed | request_id={request_id} | errors={serializer.errors}")
            return Response(
                {"error": "Validation failed", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        validated_data = serializer.validated_data
        selectors_to_heal = validated_data['selectors']
        
        results = []
        success_count = 0
        fail_count = 0

        heal_view = HealAPIView()

        logger.info(f"Batch heal started | request_id={request_id} | count={len(selectors_to_heal)}")

        for idx, selector_data in enumerate(selectors_to_heal):
            try:
                item_start_time = time.time()
                item_request_id = f"{request_id}-{idx}"

                result = heal_view._process_heal_request(
                    selector_data,
                    item_request_id,
                    item_start_time,
                    client=client,
                )

                healer_request = heal_view._save_heal_request(
                    selector_data,
                    result,
                    user=user,
                    client=client,
                )

                result['id'] = healer_request.id
                # batch_id retained for client back-compat; always 0 now that batches are not persisted.
                result['batch_id'] = 0

                results.append(result)
                success_count += 1

            except Exception as e:
                logger.error(
                    f"Batch item failed | request_id={request_id} | "
                    f"item={idx} | selector={selector_data.get('failed_selector')} | "
                    f"error={str(e)}"
                )
                fail_count += 1

                results.append({
                    "id": 0,
                    "batch_id": 0,
                    "message": f"Failed: {str(e)}",
                    "chosen": None,
                    "candidates": [],
                    "debug": {},
                })

        total_time = (time.time() - batch_start_time) * 1000

        logger.info(
            f"Batch heal completed | request_id={request_id} | "
            f"success={success_count} | failed={fail_count}"
        )

        return Response(
            {
                # batch_id retained for client back-compat; not persisted (no HealerRequestBatch row).
                "id": 0,
                "results": results,
                "total_processed": len(selectors_to_heal),
                "total_succeeded": success_count,
                "total_failed": fail_count,
                "processing_time_ms": total_time,
            },
            status=status.HTTP_200_OK,
        )
