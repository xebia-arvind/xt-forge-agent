# healer_service/main.py
import time
from fastapi import FastAPI, HTTPException, Query
from bs4 import BeautifulSoup
from typing import List, Dict, Any
from models import (
    HealRequest,
    BatchHealRequest,
    BatchHealResponse,
    HealResponse
)
from typing import List, Optional, Dict, Any
from logger import RequestLogger
from dom_extractor import DOMExtractor
from matching_engine  import MatchingEngine

app = FastAPI(
    title="Selector Healer Service",
    description="AI-powered selector healing service for test automation",
    version="1.0.0"
)



def build_custom_heal_response(
    engine_results: list,
    request_id: str,
    processing_time: float,
    vision_analyzed: bool = False,
    vision_analysis: Optional[Dict[str, Any]] = None
):
    candidate_strings = []
    confidence_scores = []
    candidates = []
    
    # Sort by score descending just in case
    # engine_results.sort(key=lambda x: float(x["score"]), reverse=True) 

    for r in engine_results:
        el = r["element"]
        candidate_strings.append(r["suggested"])
        confidence_scores.append(round(float(r["score"]), 4))

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
        #"detailed_candidates": candidates # Store full details in debug/metadata
    }
    
    if vision_analyzed and vision_analysis:
        debug_info["vision_model"] = vision_analysis.get("model_used")
        debug_info["vision_success"] = vision_analysis.get("success", False)

    return {
        "request_id": request_id,
        "message": "Success",
        "chosen": candidate_strings[0] if candidate_strings else None,
        "candidates": candidates,
        "debug": debug_info
    }
# ============================================================================
# API ENDPOINTS
# ============================================================================

def _process_heal_request(req: HealRequest, req_logger: RequestLogger, start_time: float):
    # Prepare DOM data for processing
    html_content = req.html  # Always preserve HTML for validation
    
    if req.semantic_dom and isinstance(req.semantic_dom, dict):
         # Use provided semantic DOM
         dom_data = req.semantic_dom
    elif req.html:
        # Fallback to extraction if not provided but HTML is
         extractor = DOMExtractor(html_content)
         dom_data = extractor.extract_semantic_dom(full_coverage=True)
    else:
        # Should be caught by validator but just in case
        raise ValueError("No DOM source provided")

    engine = MatchingEngine(dom_data['elements'])
    
    # Calculate processing time so far for the ranking
    # Note: original code calculated time before the second rank call
    
    results = engine.rank(
        req.failed_selector,
        req.use_of_selector,
        top_k=5
    )
    
    processing_time = (time.time() - start_time) * 1000

    response = build_custom_heal_response(
        engine_results=results,
        request_id=req_logger.request_id,
        processing_time=processing_time
    )

    return response

@app.post("/heal", response_model=HealResponse)
async def heal(req: HealRequest):
    """Heal a single failing selector"""
    start_time = time.time()
    
    with RequestLogger("POST /heal", {"selector": req.failed_selector}) as req_logger:
        try:
            return _process_heal_request(req, req_logger, start_time)
            
        except Exception as e:
            req_logger.log_error(f"Healing failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Healing failed: {str(e)}")

@app.post("/heal/batch", response_model=BatchHealResponse)
async def batch_heal(batch_req: BatchHealRequest):
    """Heal multiple falling selectors in a batch"""
    batch_start_time = time.time()
    results = []
    success_count = 0
    fail_count = 0
    
    # We use a system/batch logger for the main request
    with RequestLogger("POST /heal/batch", {"count": len(batch_req.selectors)}) as batch_logger:
        
        for req in batch_req.selectors:
            try:
                # We can reuse the logic. 
                # Note: passing batch_logger might mix logs, but simpler for now.
                # Ideally we want a per-item context but RequestLogger is designed as context manager.
                # We'll just create a child logger logically or just use the helper.
                
                # For individual item logging we can create a sub-logger or just process it.
                # Let's just call the helper.
                
                # We need a unique request ID for each if possible, or share the batch one.
                # The helper uses req_logger.request_id.
                
                # Let's just treat each as a sub-operation.
                res = _process_heal_request(req, batch_logger, time.time())
                results.append(res)
                success_count += 1
                
            except Exception as e:
                batch_logger.log_error(f"Failed to heal selector {req.failed_selector}: {e}")
                fail_count += 1
                # Add a failed response placeholder if needed, or just skip?
                # The response model expects HealResponse objects in 'results' list.
                # We should probably return an error object or structure. 
                # But HealResponse has 'message', 'candidates' etc.
                
                results.append({
                    "request_id": batch_logger.request_id,
                    "message": f"Failed: {str(e)}",
                    "chosen": None,
                    "candidates": [],
                    "debug": {}
                })

        total_time = (time.time() - batch_start_time) * 1000
        
        return {
            "request_id": batch_logger.request_id,
            "results": results,
            "total_processed": len(batch_req.selectors),
            "total_succeeded": success_count,
            "total_failed": fail_count,
            "processing_time_ms": total_time
        }    


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting Healer Service...", extra={'request_id': 'system'})
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)