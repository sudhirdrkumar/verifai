from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "QC-BKP Modern Platform"
    app_env: str = "development"
    api_v1_prefix: str = "/api/v1"

    pg_host: str = "127.0.0.1"
    pg_port: int = 5432
    pg_user: str = "postgres"
    pg_password: str = "postgres"
    pg_database: str = "qc_bkp_modern"
    pg_admin_database: str = "postgres"

    legacy_db_host: str = Field(default="127.0.0.1", validation_alias=AliasChoices("LEGACY_DB_HOST"))
    legacy_db_port: int = Field(default=3306, validation_alias=AliasChoices("LEGACY_DB_PORT"))
    legacy_db_name: str = Field(default="teamrigh_qc", validation_alias=AliasChoices("LEGACY_DB_NAME"))
    legacy_db_user: str = Field(default="teamrigh_qc", validation_alias=AliasChoices("LEGACY_DB_USER"))
    legacy_db_pass: str = Field(default="", validation_alias=AliasChoices("LEGACY_DB_PASS"))

    s3_region: str = Field(
        default="ap-south-1",
        validation_alias=AliasChoices("S3_REGION", "AWS_REGION"),
    )
    s3_bucket: str | None = Field(
        default=None,
        validation_alias=AliasChoices("S3_BUCKET", "AWS_S3_BUCKET"),
    )
    s3_access_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("S3_ACCESS_KEY", "AWS_ACCESS_KEY_ID"),
    )
    s3_secret_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("S3_SECRET_KEY", "AWS_SECRET_ACCESS_KEY"),
    )
    s3_endpoint_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("S3_ENDPOINT_URL", "AWS_ENDPOINT_URL"),
    )

    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY"),
    )
    openai_model: str = Field(
        default="gpt-4o-mini",
        validation_alias=AliasChoices("OPENAI_MODEL"),
    )
    openai_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_BASE_URL"),
    )
    openai_embedding_model: str = Field(
        default="text-embedding-3-small",
        validation_alias=AliasChoices("OPENAI_EMBEDDING_MODEL"),
    )
    openai_rag_model: str = Field(
        default="gpt-4o-mini",
        validation_alias=AliasChoices("OPENAI_RAG_MODEL"),
    )
    deepseek_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "DEEPSEEK_OCR_API_KEY"),
    )
    deepseek_model: str = Field(
        default="deepseek-v3.2",
        validation_alias=AliasChoices("DEEPSEEK_MODEL", "DEEPSEEK_OCR_MODEL"),
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com/v1",
        validation_alias=AliasChoices("DEEPSEEK_BASE_URL", "DEEPSEEK_OCR_BASE_URL"),
    )
    deepseek_ocr_medical: bool = Field(
        default=False,
        validation_alias=AliasChoices("DEEPSEEK_OCR_MEDICAL"),
    )

    ocr_space_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OCR_SPACE_API_KEY"),
    )
    ocr_space_endpoint: str = Field(
        default="https://api.ocr.space/parse/image",
        validation_alias=AliasChoices("OCR_SPACE_ENDPOINT"),
    )

    aws_textract_region: str = Field(
        default="ap-south-1",
        validation_alias=AliasChoices("AWS_TEXTRACT_REGION", "TEXTRACT_REGION", "AWS_REGION", "S3_REGION"),
    )
    aws_textract_access_key_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AWS_TEXTRACT_ACCESS_KEY_ID", "TEXTRACT_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID", "S3_ACCESS_KEY"),
    )
    aws_textract_secret_access_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AWS_TEXTRACT_SECRET_ACCESS_KEY", "TEXTRACT_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY", "S3_SECRET_KEY"),
    )
    aws_textract_session_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AWS_TEXTRACT_SESSION_TOKEN", "TEXTRACT_SESSION_TOKEN", "AWS_SESSION_TOKEN"),
    )
    aws_textract_endpoint_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AWS_TEXTRACT_ENDPOINT_URL", "TEXTRACT_ENDPOINT_URL", "AWS_ENDPOINT_URL", "S3_ENDPOINT_URL"),
    )

    auth_session_hours: int = Field(
        default=12,
        validation_alias=AliasChoices("AUTH_SESSION_HOURS"),
    )
    bootstrap_admin_username: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BOOTSTRAP_ADMIN_USERNAME"),
    )
    bootstrap_admin_password: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BOOTSTRAP_ADMIN_PASSWORD"),
    )
    ocr_space_engine: int = Field(
        default=2,
        validation_alias=AliasChoices("OCR_SPACE_ENGINE"),
    )

    teamrightworks_integration_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TEAMRIGHTWORKS_INTEGRATION_TOKEN", "INTEGRATION_TEAMRIGHTWORKS_TOKEN"),
    )
    teamrightworks_integration_actor: str = Field(
        default="integration:teamrightworks",
        validation_alias=AliasChoices("TEAMRIGHTWORKS_INTEGRATION_ACTOR", "INTEGRATION_TEAMRIGHTWORKS_ACTOR"),
    )
    teamrightworks_sync_trigger_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TEAMRIGHTWORKS_SYNC_TRIGGER_URL"),
    )
    teamrightworks_sync_trigger_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TEAMRIGHTWORKS_SYNC_TRIGGER_KEY"),
    )
    teamrightworks_sync_default_password: str = Field(
        default="ChangeMe123A",
        validation_alias=AliasChoices("TEAMRIGHTWORKS_SYNC_DEFAULT_PASSWORD"),
    )
    verifai_intake_url: str = Field(
        default="http://13.205.207.234:8000/api/v1/claims/intake",
        validation_alias=AliasChoices("VERIFAI_INTAKE_URL"),
    )
    verifai_source_application: str = Field(
        default="qc-python",
        validation_alias=AliasChoices("VERIFAI_SOURCE_APPLICATION"),
    )
    verifai_insurance_company_code: str = Field(
        default="QC_BKP",
        validation_alias=AliasChoices("VERIFAI_INSURANCE_COMPANY_CODE"),
    )
    verifai_callback_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("VERIFAI_CALLBACK_URL"),
    )
    verifai_callback_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices("VERIFAI_CALLBACK_SECRET"),
    )
    verifai_timeout_seconds: float = Field(
        default=30.0,
        validation_alias=AliasChoices("VERIFAI_TIMEOUT_SECONDS"),
    )
    verifai_presigned_url_expires_in: int = Field(
        default=86400,
        validation_alias=AliasChoices("VERIFAI_PRESIGNED_URL_EXPIRES_IN"),
    )
    ml_auto_retrain_on_qc_yes: bool = Field(
        default=True,
        validation_alias=AliasChoices("ML_AUTO_RETRAIN_ON_QC_YES"),
    )
    ml_auto_retrain_min_interval_minutes: int = Field(
        default=30,
        validation_alias=AliasChoices("ML_AUTO_RETRAIN_MIN_INTERVAL_MINUTES"),
    )
    drug_lookup_api_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("DRUG_LOOKUP_API_ENABLED"),
    )
    drug_lookup_api_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DRUG_LOOKUP_API_URL"),
    )
    drug_lookup_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DRUG_LOOKUP_API_KEY"),
    )
    drug_lookup_api_timeout_seconds: float = Field(
        default=8.0,
        validation_alias=AliasChoices("DRUG_LOOKUP_API_TIMEOUT_SECONDS"),
    )
    drug_lookup_use_rxnav_fallback: bool = Field(
        default=True,
        validation_alias=AliasChoices("DRUG_LOOKUP_USE_RXNAV_FALLBACK"),
    )
    medicine_rectify_scheduler_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("MEDICINE_RECTIFY_SCHEDULER_ENABLED"),
    )
    medicine_rectify_scheduler_tz: str = Field(
        default="Asia/Kolkata",
        validation_alias=AliasChoices("MEDICINE_RECTIFY_SCHEDULER_TZ"),
    )
    medicine_rectify_scheduler_hour: int = Field(
        default=1,
        validation_alias=AliasChoices("MEDICINE_RECTIFY_SCHEDULER_HOUR"),
    )
    medicine_rectify_scheduler_minute: int = Field(
        default=0,
        validation_alias=AliasChoices("MEDICINE_RECTIFY_SCHEDULER_MINUTE"),
    )
    medicine_rectify_min_score: float = Field(
        default=0.82,
        validation_alias=AliasChoices("MEDICINE_RECTIFY_MIN_SCORE"),
    )
    medicine_rectify_sleep_ms: int = Field(
        default=0,
        validation_alias=AliasChoices("MEDICINE_RECTIFY_SLEEP_MS"),
    )
    razorpay_ifsc_verify_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("RAZORPAY_IFSC_VERIFY_ENABLED"),
    )
    razorpay_ifsc_api_base_url: str = Field(
        default="https://ifsc.razorpay.com",
        validation_alias=AliasChoices("RAZORPAY_IFSC_API_BASE_URL"),
    )
    razorpay_ifsc_timeout_seconds: float = Field(
        default=8.0,
        validation_alias=AliasChoices("RAZORPAY_IFSC_TIMEOUT_SECONDS"),
    )
    cleartax_gst_verify_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CLEARTAX_GST_VERIFY_ENABLED"),
    )
    cleartax_gst_base_url: str = Field(
        default="https://cleartax.in",
        validation_alias=AliasChoices("CLEARTAX_GST_BASE_URL"),
    )
    cleartax_gst_timeout_seconds: float = Field(
        default=10.0,
        validation_alias=AliasChoices("CLEARTAX_GST_TIMEOUT_SECONDS"),
    )
    medicine_rectify_timeout_seconds: int = Field(
        default=10800,
        validation_alias=AliasChoices("MEDICINE_RECTIFY_TIMEOUT_SECONDS"),
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def sqlalchemy_database_uri(self) -> str:
        return (
            f"postgresql+psycopg://{self.pg_user}:{self.pg_password}@"
            f"{self.pg_host}:{self.pg_port}/{self.pg_database}?connect_timeout=5"
        )

    @property
    def psycopg_database_uri(self) -> str:
        return (
            f"postgresql://{self.pg_user}:{self.pg_password}@"
            f"{self.pg_host}:{self.pg_port}/{self.pg_database}?connect_timeout=5"
        )

    @property
    def psycopg_admin_uri(self) -> str:
        return (
            f"postgresql://{self.pg_user}:{self.pg_password}@"
            f"{self.pg_host}:{self.pg_port}/{self.pg_admin_database}?connect_timeout=5"
        )


settings = Settings()

