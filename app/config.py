from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore" so stray/retired env vars (e.g. the removed Sheets keys) on a
    # host or in .env don't crash startup with "extra inputs not permitted".
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./smart_vend.db"
    debug: bool = False
    app_title: str = "Prime Vending"

    # AI / Lead generation
    anthropic_api_key: str = ""
    tavily_api_key: str = ""
    # Firecrawl — structured web scraping for price comparison & equipment images.
    # When unset, scrapers fall back to BeautifulSoup HTML parsing.
    firecrawl_api_key: str = ""

    # Market-reference pricing (read-only "what competitors sell similar items
    # for"; never feeds cost/margin math). All optional — each source degrades to
    # "no data" when its key is missing.
    #   • UPCitemdb: barcode → price band. The keyless trial endpoint works at
    #     100 lookups/day; a paid key raises the limit and is used when set.
    #   • eBay Browse: live/sold listings for similar items (OAuth client creds).
    #   • BLS: regional/national average price for a few tracked categories.
    upcitemdb_api_key: str = ""
    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    bls_api_key: str = ""

    # SAM.gov public API key — powers the Gov Contracts tab in Lead Gen (live
    # federal contract-opportunity search). Optional: when unset, the search
    # degrades to "not configured" instead of erroring. Get a key at
    # https://open.sam.gov/ (Account Details → Public API Key).
    sam_gov_api_key: str = ""
    gmail_user: str = ""
    gmail_app_password: str = ""
    # Google Calendar "Appointment Schedule" public booking page URL. The chatbot
    # shows live open slots (computed from the calendar's free/busy via
    # app/services/google_calendar.py) and links here for the customer to confirm.
    google_booking_url: str = ""

    # Additional AI providers for the customer service chatbot
    groq_api_key: str = ""
    gemini_api_key: str = ""
    openai_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434/v1"

    company_blurb: str = (
        "Prime Vending is a veteran-owned smart cooler vending company "
        "serving Bay County, FL. We provide modern, cashless smart cooler "
        "machines to gyms, hotels, corporate offices, and other high-traffic venues."
    )

    # Google OAuth (for staff authentication)
    google_client_id: str = ""
    google_client_secret: str = ""
    # Generate with: python -c "import secrets; print(secrets.token_hex(32))"
    session_secret_key: str = "change-me-in-production"  # noqa: S105 — placeholder; real value from env
    # Comma-separated Gmail addresses allowed to access internal app.
    # Leave empty to allow any Google-authenticated user (not recommended for production).
    allowed_emails: str = ""


settings = Settings()
