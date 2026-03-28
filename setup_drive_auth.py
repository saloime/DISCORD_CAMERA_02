"""
One-time setup script to get a Google Drive OAuth2 refresh token.
Run this locally in a browser, sign in with your Google account,
and it will print a refresh token to add to your .env file.
"""
import json
from google_auth_oauthlib.flow import InstalledAppFlow

# You need OAuth client credentials from Google Cloud Console.
# Go to: https://console.cloud.google.com/apis/credentials?project=gen-lang-client-0352038035
# Click "Create Credentials" → "OAuth client ID" → "Desktop app"
# Download the JSON and save it as client_secret_1025457723738-npjerpht8os8jnhmtci5752g5h9hi4rm.apps.googleusercontent.com.json in this folder.

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

flow = InstalledAppFlow.from_client_secrets_file("client_secret_1025457723738-npjerpht8os8jnhmtci5752g5h9hi4rm.apps.googleusercontent.com.json", SCOPES)
creds = flow.run_local_server(port=0)

print("\n=== Add these to your .env file ===")
print(f"GOOGLE_CLIENT_ID_DISCORD_BOT={creds.client_id}")
print(f"GOOGLE_CLIENT_SECRET_DISCORD_BOT={creds.client_secret}")
print(f"GOOGLE_REFRESH_TOKEN_DISCORD_BOT={creds.refresh_token}")
print("\nAlso set these same values on Render.")
