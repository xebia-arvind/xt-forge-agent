from rest_framework_simplejwt.tokens import RefreshToken

def generate_tokens(user, client):
    refresh = RefreshToken.for_user(user)

    # Custom claims
    refresh["client_id"] = str(client.secret_key)
    refresh["email"] = user.email

    return {
        "refresh": str(refresh),
        "access": str(refresh.access_token),
    }
