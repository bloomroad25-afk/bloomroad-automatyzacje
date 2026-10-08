"""Tylko jesli NIE masz pliku token.json po poprzednim skrypcie.
Wymaga credentials.json (Google Cloud > OAuth klient typu "Aplikacja na komputer")
w tym samym folderze. Zaloguj sie kontem bloomroad25 (NIE prywatnym)."""
from google_auth_oauthlib.flow import InstalledAppFlow

flow = InstalledAppFlow.from_client_secrets_file(
    "credentials.json", ["https://www.googleapis.com/auth/drive"])
creds = flow.run_local_server(port=0)
open("token.json", "w").write(creds.to_json())
print("Gotowe: zapisano token.json")
