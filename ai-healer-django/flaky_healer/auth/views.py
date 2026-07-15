from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from .serializers import LoginSerializer
from .tokens import generate_tokens

import logging
logger = logging.getLogger("auth")

class LoginAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(
            data=request.data,
            context={"request": request}
        )
        try:
            serializer.is_valid(raise_exception=True)
        except Exception as e:
            logger.warning(
                "LOGIN FAILED | ip=%s | data=%s | error=%s",
                self.get_client_ip(request),
                self.mask_sensitive(request.data),
                str(e),
            )
            raise

        user = serializer.validated_data["user"]
        client = serializer.validated_data["client"]

        logger.info(
            "LOGIN SUCCESS | user=%s | client=%s | ip=%s",
            user.email,
            str(client.secret_key),
            self.get_client_ip(request),
        )

        tokens = generate_tokens(user, client)

        return Response({
            "tokens": tokens,
            "user": {
                "id": user.id,
                "email": user.email,
            },
            "client": {
                "id": str(client.secret_key),
                "name": client.clientname,
            }
        })

    def get_client_ip(self, request):
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            return x_forwarded_for.split(",")[0]
        return request.META.get("REMOTE_ADDR")

    def mask_sensitive(self, data):
        return {
            "email": data.get("email"),
            "client_secret": str(data.get("client_secret"))[:8] + "****"
            if data.get("client_secret") else None
        }
