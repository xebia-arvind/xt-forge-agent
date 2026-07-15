
import os
import django
import uuid

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'flaky_healer.settings')
django.setup()

from django.contrib.auth import get_user_model
from clients.models import Clients, UserClient

User = get_user_model()

def setup_user():
    email = "test@example.com"
    password = "password123"
    client_name = "TestClient"
    
    # 1. Create or Get User
    user, created = User.objects.get_or_create(email=email)
    if created:
        user.set_password(password)
        user.save()
        print(f"Created user: {email}")
    else:
        # Reset password to ensure we know it
        user.set_password(password)
        user.save()
        print(f"Updated user: {email}")

    # 2. Create or Get Client
    client, created = Clients.objects.get_or_create(clientname=client_name)
    if created:
        print(f"Created client: {client_name}")
    else:
        print(f"Found client: {client_name}")
    
    # Ensure secret key is accessible (it might be auto-generated)
    # If using UUIDField with auto=True, it's there.
    print(f"Client Secret: {client.secret_key}")

    # 3. specific Logic for UserClient
    # Check if UserClient exists
    if not UserClient.objects.filter(user=user, clients=client).exists():
        UserClient.objects.create(user=user, clients=client)
        print(f"Linked user {email} to client {client_name}")
    else:
        print(f"User {email} already linked to client {client_name}")
        
    return email, password, str(client.secret_key)

if __name__ == "__main__":
    email, password, secret = setup_user()
    print("\nCREDENTIALS FOR TEST:")
    print(f"Email: {email}")
    print(f"Password: {password}")
    print(f"Client Secret: {secret}")
