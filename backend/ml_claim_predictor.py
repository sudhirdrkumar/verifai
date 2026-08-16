#!/usr/bin/env python3
"""
ML Predictor for Stage 3 Report Generation
Uses trained model to predict claim decisions with confidence scores
"""

import os
import logging

logger = logging.getLogger(__name__)

# Recommendation mapping
RECOMMENDATION_MAP = {
    0: 'approve',
    1: 'reject',
    2: 'need_more_evidence'
}


class ClaimPredictor:
    """Predict claim recommendations using ML model"""

    def __init__(self, model_path='ml_models/claim_classifier.pkl'):
        self.classifier = None
        self._pandas = None
        self.model_available = os.path.exists(model_path)

        if self.model_available:
            try:
                import pandas as pd
                from ml_claim_classifier import ClaimClassifier

                self._pandas = pd
                self.classifier = ClaimClassifier(model_path)
                self.classifier.load()
                logger.info('✅ ML model loaded successfully')
            except Exception as e:
                logger.warning(f'Failed to load model: {e}')
                self.model_available = False
        else:
            logger.warning(f'Model not found at {model_path} - ML predictions disabled')

    def extract_features(self, structured_json):
        """Extract features from structured claim data"""
        features = {}

        # Text fields
        diagnosis = str(structured_json.get('diagnosis', '')).strip()
        medicine_used = str(structured_json.get('medicine_used', '')).strip()
        complaints = str(structured_json.get('complaints', '')).strip()
        investigations = str(structured_json.get('investigation_finding_in_details', '')).strip()
        high_end_antibiotic = str(structured_json.get('high_end_antibiotic_for_rejection', '')).strip()
        deranged_investigation = str(structured_json.get('deranged_investigation', '')).strip()

        # Length features
        features['diagnosis_len'] = len(diagnosis)
        features['medicine_len'] = len(medicine_used)
        features['complaints_len'] = len(complaints)
        features['investigations_len'] = len(investigations)

        # Numeric features
        claim_amount_str = str(structured_json.get('claim_amount', '0')).replace(',', '').replace('₹', '')
        try:
            features['claim_amount'] = float(claim_amount_str)
        except (ValueError, TypeError):
            features['claim_amount'] = 0.0

        # Boolean features
        features['has_high_end_antibiotic'] = len(high_end_antibiotic) > 0
        features['has_deranged_investigation'] = len(deranged_investigation) > 0

        # Risk scores
        try:
            features['fraud_risk_score'] = float(structured_json.get('fraud_risk_score', 0))
        except (ValueError, TypeError):
            features['fraud_risk_score'] = 0.0

        try:
            features['qc_risk_score'] = float(structured_json.get('qc_risk_score', 0))
        except (ValueError, TypeError):
            features['qc_risk_score'] = 0.0

        # Medical keywords
        diagnosis_lower = diagnosis.lower()
        features['has_surgery'] = bool(any(
            word in diagnosis_lower
            for word in ['surgery', 'surgical', 'procedure', 'operation', 'orif', 'fixation', 'lscs', 'caesarean']
        ))

        features['has_infection'] = bool(any(
            word in diagnosis_lower
            for word in ['infection', 'septic', 'pneumonia', 'fever', 'abscess']
        ))

        features['has_critical'] = bool(any(
            word in diagnosis_lower
            for word in ['critical', 'acute', 'severe', 'emergency', 'shock', 'coma']
        ))

        return features

    def predict(self, structured_json):
        """
        Predict claim recommendation using ML model

        Args:
            structured_json: Structured claim data from Stage 2

        Returns:
            {
                'recommendation': 'approve|reject|need_more_evidence',
                'confidence': 0.0-1.0,
                'probabilities': {
                    'approve': float,
                    'reject': float,
                    'need_more_evidence': float
                },
                'features_used': dict
            }
        """
        if not self.model_available:
            logger.warning('ML model not available, returning None')
            return None

        try:
            # Extract features
            features = self.extract_features(structured_json)

            # Create DataFrame for prediction
            X = self._pandas.DataFrame([features])

            # Ensure feature order matches training
            expected_features = self.classifier.feature_names
            if expected_features:
                X = X[expected_features]

            # Make prediction
            prediction, probabilities = self.classifier.predict(X)

            rec_idx = prediction[0]
            recommendation = RECOMMENDATION_MAP.get(rec_idx, 'need_more_evidence')
            confidence = float(probabilities[0][rec_idx])

            # Map probabilities
            prob_map = {
                'approve': float(probabilities[0][0]),
                'reject': float(probabilities[0][1]),
                'need_more_evidence': float(probabilities[0][2])
            }

            result = {
                'recommendation': recommendation,
                'confidence': confidence,
                'probabilities': prob_map,
                'features_used': features
            }

            logger.debug(f'ML prediction: {recommendation} (confidence: {confidence:.2%})')
            return result

        except Exception as e:
            logger.error(f'ML prediction error: {e}')
            return None


# Global predictor instance
_predictor_instance = None


def get_predictor():
    """Get or create global predictor instance"""
    global _predictor_instance
    if _predictor_instance is None:
        model_path = os.path.join(os.path.dirname(__file__), 'ml_models', 'claim_classifier.pkl')
        _predictor_instance = ClaimPredictor(model_path)
    return _predictor_instance


def predict_claim(structured_json):
    """
    Convenience function to predict claim recommendation

    Args:
        structured_json: Structured claim data

    Returns:
        ML prediction result or None if model unavailable
    """
    predictor = get_predictor()
    return predictor.predict(structured_json)
