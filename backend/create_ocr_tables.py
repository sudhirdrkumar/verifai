#!/usr/bin/env python3
"""
Migration script to create OCR pipeline tables
Run this on EC2: python create_ocr_tables.py
"""

import os
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

# Database connection from environment or defaults
DB_HOST = os.getenv('PG_HOST', '127.0.0.1')
DB_PORT = os.getenv('PG_PORT', '5432')
DB_USER = os.getenv('PG_USER', 'admin')
DB_PASSWORD = os.getenv('PG_PASSWORD', 'localpassword')
DB_NAME = os.getenv('PG_DATABASE', 'qc_bkp_modern_live')

def run_migration():
    """Create all necessary tables for OCR pipeline"""

    try:
        # Connect to database
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cur = conn.cursor()

        print("Creating OCR pipeline tables...")

        # Create document_extractions table
        print("✓ Creating document_extractions table...")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS document_extractions (
                id SERIAL PRIMARY KEY,
                document_id BIGINT NOT NULL,
                raw_response TEXT,
                model_name VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT fk_document_id
                    FOREIGN KEY (document_id)
                    REFERENCES claim_documents(id)
                    ON DELETE CASCADE
            );
        """)

        # Create claim_structured_data table
        print("✓ Creating claim_structured_data table...")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS claim_structured_data (
                id SERIAL PRIMARY KEY,
                claim_id BIGINT NOT NULL UNIQUE,
                normalized_fields JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT fk_claim_id
                    FOREIGN KEY (claim_id)
                    REFERENCES claims(id)
                    ON DELETE CASCADE
            );
        """)

        # Create indexes
        print("✓ Creating indexes...")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_doc_extractions_doc_id ON document_extractions(document_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_doc_extractions_created ON document_extractions(created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_claim_structured_created ON claim_structured_data(created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_claim_structured_claim_id ON claim_structured_data(claim_id);")

        # Verify tables exist
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_name IN ('document_extractions', 'claim_structured_data');
        """)
        tables = cur.fetchall()

        if len(tables) >= 2:
            print("\n✅ Migration successful!")
            print(f"   Tables created: {', '.join([t[0] for t in tables])}")
        else:
            print("\n⚠️ Warning: Some tables may not have been created")
            print(f"   Found tables: {', '.join([t[0] for t in tables])}")

        cur.close()
        conn.close()

    except Exception as e:
        print(f"❌ Migration failed: {str(e)}")
        raise

if __name__ == "__main__":
    run_migration()
