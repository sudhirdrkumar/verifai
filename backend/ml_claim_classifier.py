#!/usr/bin/env python3
"""
ML-based Claim Classifier for Stage 3 Report Generation
Trains on 10k+ claims to predict APPROVE/REJECT/NEED_MORE_EVIDENCE decisions
"""

import os
import json
import pickle
import logging
import numpy as np
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import LabelEncoder
from sklearn.metrics import classification_report, confusion_matrix, accuracy_score
import psycopg

env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(env_path)

logger = logging.getLogger(__name__)

# Database config
DB_DSN = os.getenv('DATABASE_URL')
if not DB_DSN:
    pg_host = os.getenv('PG_HOST', '127.0.0.1')
    pg_port = os.getenv('PG_PORT', '5432')
    pg_user = os.getenv('PG_USER', 'postgres')
    pg_password = os.getenv('PG_PASSWORD', '')
    pg_database = os.getenv('PG_DATABASE', 'qc_bkp_modern')
    DB_DSN = f'postgresql://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_database}'


class ClaimDataLoader:
    """Load claim data from PostgreSQL for ML training"""

    def __init__(self, db_dsn):
        self.db_dsn = db_dsn

    def load_training_data(self):
        """Load structured claims data with decisions"""
        conn = psycopg.connect(self.db_dsn)
        cur = conn.cursor()

        # Join structured data with active decisions
        query = '''
        SELECT
            csd.diagnosis,
            csd.complaints,
            csd.findings,
            csd.medicine_used,
            csd.claim_amount,
            csd.investigation_finding_in_details,
            csd.deranged_investigation,
            csd.high_end_antibiotic_for_rejection,
            dr.recommendation,
            dr.fraud_risk_score,
            dr.qc_risk_score
        FROM claim_structured_data csd
        JOIN decision_results dr ON csd.claim_id = dr.claim_id
        WHERE dr.is_active = true
        AND csd.diagnosis IS NOT NULL
        AND csd.diagnosis != ''
        ORDER BY dr.generated_at DESC
        '''

        cur.execute(query)
        rows = cur.fetchall()
        conn.close()

        # Convert to DataFrame
        columns = [
            'diagnosis', 'complaints', 'findings', 'medicine_used', 'claim_amount',
            'investigations', 'deranged_investigation', 'high_end_antibiotic', 'recommendation',
            'fraud_risk_score', 'qc_risk_score'
        ]
        df = pd.DataFrame(rows, columns=columns)

        logger.info(f'Loaded {len(df)} records from database')
        logger.info(f'Recommendations distribution:\n{df["recommendation"].value_counts()}')

        return df

    def prepare_features(self, df):
        """Extract and engineer features from structured data"""

        # Create feature DataFrame
        X = pd.DataFrame()

        # Text length features
        X['diagnosis_len'] = df['diagnosis'].fillna('').astype(str).str.len()
        X['medicine_len'] = df['medicine_used'].fillna('').astype(str).str.len()
        X['complaints_len'] = df['complaints'].fillna('').astype(str).str.len()
        X['investigations_len'] = df['investigations'].fillna('').astype(str).str.len()

        # Extract claim amount as numeric
        X['claim_amount'] = pd.to_numeric(
            df['claim_amount'].astype(str).str.replace(',', '').str.replace('₹', ''),
            errors='coerce'
        ).fillna(0)

        # Categorical features
        X['has_high_end_antibiotic'] = df['high_end_antibiotic'].fillna('').astype(str).str.len() > 0
        X['has_deranged_investigation'] = df['deranged_investigation'].fillna('').astype(str).str.len() > 0

        # Risk scores
        X['fraud_risk_score'] = pd.to_numeric(df['fraud_risk_score'], errors='coerce').fillna(0)
        X['qc_risk_score'] = pd.to_numeric(df['qc_risk_score'], errors='coerce').fillna(0)

        # Medical keywords in diagnosis
        X['has_surgery'] = df['diagnosis'].fillna('').astype(str).str.contains(
            r'\b(surgery|surgical|procedure|operation|orif|fixation|lscs|caesarean)\b',
            case=False, regex=True
        ).astype(int)

        X['has_infection'] = df['diagnosis'].fillna('').astype(str).str.contains(
            r'\b(infection|septic|pneumonia|fever|abscess)\b',
            case=False, regex=True
        ).astype(int)

        X['has_critical'] = df['diagnosis'].fillna('').astype(str).str.contains(
            r'\b(critical|acute|severe|emergency|shock|coma)\b',
            case=False, regex=True
        ).astype(int)

        # Target variable
        y = df['recommendation'].map({
            'approve': 0,
            'reject': 1,
            'need_more_evidence': 2
        })

        logger.info(f'Features shape: {X.shape}')
        logger.info(f'Target distribution:\n{y.value_counts()}')

        return X, y


class ClaimClassifier:
    """ML classifier for claim decisions"""

    def __init__(self, model_path='ml_models/claim_classifier.pkl'):
        self.model_path = model_path
        self.model = None
        self.le = LabelEncoder()
        self.feature_names = None
        os.makedirs(os.path.dirname(model_path), exist_ok=True)

    def train(self, X_train, y_train):
        """Train the classification model"""
        logger.info('Training Random Forest model...')

        self.model = RandomForestClassifier(
            n_estimators=100,
            max_depth=15,
            min_samples_split=10,
            min_samples_leaf=5,
            random_state=42,
            n_jobs=-1,
            class_weight='balanced'  # Handle imbalanced data
        )

        self.model.fit(X_train, y_train)
        self.feature_names = X_train.columns.tolist()

        logger.info('Model training complete')

    def evaluate(self, X_test, y_test):
        """Evaluate model on test set"""
        y_pred = self.model.predict(X_test)

        logger.info('Model Evaluation:')
        logger.info(f'Accuracy: {accuracy_score(y_test, y_pred):.4f}')
        logger.info(f'\nClassification Report:\n{classification_report(y_test, y_pred)}')
        logger.info(f'\nConfusion Matrix:\n{confusion_matrix(y_test, y_pred)}')

    def predict(self, X):
        """Predict on new data"""
        if self.model is None:
            self.load()

        predictions = self.model.predict(X)
        probabilities = self.model.predict_proba(X)

        return predictions, probabilities

    def save(self):
        """Save model to disk"""
        if self.model is None:
            raise ValueError('No model to save')

        model_data = {
            'model': self.model,
            'feature_names': self.feature_names,
            'le': self.le
        }

        with open(self.model_path, 'wb') as f:
            pickle.dump(model_data, f)

        logger.info(f'Model saved to {self.model_path}')

    def load(self):
        """Load model from disk"""
        if not os.path.exists(self.model_path):
            raise FileNotFoundError(f'Model not found at {self.model_path}')

        with open(self.model_path, 'rb') as f:
            model_data = pickle.load(f)

        self.model = model_data['model']
        self.feature_names = model_data['feature_names']
        self.le = model_data['le']

        logger.info(f'Model loaded from {self.model_path}')


def train_model():
    """Main training pipeline"""
    logging.basicConfig(level=logging.INFO)

    logger.info('Starting claim classifier training...')

    # Load data
    loader = ClaimDataLoader(DB_DSN)
    df = loader.load_training_data()

    # Prepare features
    X, y = loader.prepare_features(df)

    # Split data
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )

    logger.info(f'Training set: {len(X_train)}, Test set: {len(X_test)}')

    # Train model
    classifier = ClaimClassifier()
    classifier.train(X_train, y_train)
    classifier.evaluate(X_test, y_test)
    classifier.save()

    logger.info('✅ Model training complete')


if __name__ == '__main__':
    train_model()
