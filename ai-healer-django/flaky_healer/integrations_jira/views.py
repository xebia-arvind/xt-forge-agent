from __future__ import annotations

from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from clients.mixins import require_client

from .models import JiraConnection
from .serializers import JiraCommentSerializer, JiraConnectionSerializer, JiraSearchSerializer
from .services import JiraClient, JiraError, build_adf_paragraph


class _ClientScopedAPIView(APIView):
    authentication_classes = [JWTAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]


def _get_connection(client) -> JiraConnection | None:
    return JiraConnection.objects.filter(client=client).first()


class JiraConnectionView(_ClientScopedAPIView):
    """GET the current tenant's Jira credentials (any member).
    PUT/POST/DELETE are admin-only — regular users cannot rotate the token
    through the dashboard. Admins do it via Django admin.
    """

    def get(self, request):
        client = require_client(request)
        conn = _get_connection(client)
        if not conn:
            return Response({"detail": "No Jira connection configured for this tenant."}, status=status.HTTP_404_NOT_FOUND)
        return Response(JiraConnectionSerializer(conn).data)

    def put(self, request):
        if not request.user.is_superuser:
            return Response(
                {"detail": "Only administrators may update Jira credentials. Ask your admin to update it in Django admin."},
                status=status.HTTP_403_FORBIDDEN,
            )
        client = require_client(request)
        conn = _get_connection(client)
        serializer = JiraConnectionSerializer(instance=conn, data=request.data, partial=bool(conn))
        serializer.is_valid(raise_exception=True)
        if conn is None:
            # Create path — attach the client explicitly.
            conn = JiraConnection(client=client, **{k: v for k, v in serializer.validated_data.items() if k != "api_token"})
            plaintext = serializer.validated_data.get("api_token", "")
            conn.set_api_token(plaintext)
            conn.save()
        else:
            conn = serializer.save()
        return Response(JiraConnectionSerializer(conn).data, status=status.HTTP_200_OK)


class JiraConnectionFullView(_ClientScopedAPIView):
    """GET the current tenant's Jira credentials including the decrypted API token.

    Distinct from `JiraConnectionView` (which is deliberately token-less) — this
    endpoint exists so the Streamlit UI can pre-fill Jira URL/email/token on
    login without asking the operator to type them each session. Token stays
    scoped to the caller's tenant via the same JWT/session auth stack.
    """

    def get(self, request):
        client = require_client(request)
        conn = _get_connection(client)
        if not conn:
            return Response(
                {"detail": "No Jira connection configured for this tenant."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            {
                "base_url": conn.base_url,
                "email": conn.email,
                "display_name": conn.display_name,
                "api_token": conn.get_api_token(),
            }
        )


class JiraIssueSearchView(_ClientScopedAPIView):
    def post(self, request):
        client = require_client(request)
        conn = _get_connection(client)
        if not conn:
            return Response({"detail": "No Jira connection configured."}, status=status.HTTP_400_BAD_REQUEST)
        serializer = JiraSearchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            data = JiraClient(conn).search(
                jql=serializer.validated_data["jql"],
                max_results=serializer.validated_data["max_results"],
            )
        except JiraError as e:
            return Response({"error": str(e)}, status=e.status_code)
        return Response(data)


class JiraIssueDetailView(_ClientScopedAPIView):
    def get(self, request, issue_key: str):
        client = require_client(request)
        conn = _get_connection(client)
        if not conn:
            return Response({"detail": "No Jira connection configured."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            data = JiraClient(conn).issue(issue_key)
        except JiraError as e:
            return Response({"error": str(e)}, status=e.status_code)
        return Response(data)


class JiraCommentPushView(_ClientScopedAPIView):
    def post(self, request):
        client = require_client(request)
        conn = _get_connection(client)
        if not conn:
            return Response({"detail": "No Jira connection configured."}, status=status.HTTP_400_BAD_REQUEST)
        serializer = JiraCommentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        adf = build_adf_paragraph(serializer.validated_data["body"])
        try:
            data = JiraClient(conn).add_comment(serializer.validated_data["issue_key"], adf)
        except JiraError as e:
            return Response({"error": str(e)}, status=e.status_code)
        return Response(data, status=status.HTTP_201_CREATED)
