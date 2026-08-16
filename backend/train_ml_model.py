#!/usr/bin/env python3
"""
Train ML claim classifier using 10k+ claims from PostgreSQL
Run: python train_ml_model.py
"""

import os
import sys
import logging
from ml_claim_classifier import train_model

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger(__name__)

    logger.info('=' * 60)
    logger.info('ML CLAIM CLASSIFIER TRAINING')
    logger.info('=' * 60)

    try:
        train_model()
        logger.info('✅ Training complete! Model saved to ml_models/claim_classifier.pkl')
    except Exception as e:
        logger.error(f'❌ Training failed: {e}', exc_info=True)
        sys.exit(1)
