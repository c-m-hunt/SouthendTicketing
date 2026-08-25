import os

from app import app

if __name__ == "__main__":
    # macOS hands port 5000 to AirPlay Receiver, so allow an override.
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")))
