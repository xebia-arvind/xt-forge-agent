from django.contrib.auth import authenticate
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from clients.models import Clients, UserClient
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password
User = get_user_model()


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    client_secret = serializers.UUIDField()

    def validate(self, attrs):
        email = attrs["email"]
        password = attrs["password"]
        client_secret = attrs["client_secret"]

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            raise AuthenticationFailed("Invalid email or password")

        if not check_password(password, user.password):
            raise AuthenticationFailed("Invalid email or password")

        # client validation
        try:
            client = Clients.objects.get(secret_key=client_secret)
        except Clients.DoesNotExist:
            raise AuthenticationFailed("Invalid client secret")

        if not UserClient.objects.filter(user=user, clients=client).exists():
            raise AuthenticationFailed("User not assigned to this client")

        attrs["user"] = user
        attrs["client"] = client
        return attrs
