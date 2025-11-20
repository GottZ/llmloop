import os

class Config:
    DATABASE_URL = os.getenv("DATABASE_URL")
    # Optional seeding vars (not used for app logic, just DB init)
    SEED_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    SEED_OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    
    # App settings
    DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key")
