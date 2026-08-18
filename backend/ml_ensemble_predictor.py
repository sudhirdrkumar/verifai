"""Ensemble predictor combining XGBoost and Naive Bayes models for Stage 3."""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Ensemble configuration
XGBOOST_WEIGHT = 0.55  # XGBoost: 70.4% accuracy
NAIVE_BAYES_WEIGHT = 0.45  # Naive Bayes: better on different data
CONFIDENCE_THRESHOLD = 0.55  # Min confidence to use prediction
DISAGREEMENT_THRESHOLD = 0.20  # Flag for manual review if diff > 20%


def get_xgboost_prediction(structured_json: dict) -> dict[str, Any] | None:
    """Get XGBoost model prediction."""
    try:
        from ml_claim_predictor import ClaimPredictor

        predictor = ClaimPredictor()
        result = predictor.predict(structured_json)

        if result and result.get("recommendation"):
            return {
                "label": result["recommendation"],
                "confidence": float(result.get("confidence", 0.0)),
                "model_version": result.get("model_version"),
                "model": "xgboost",
            }
    except Exception as e:
        logger.warning(f"XGBoost prediction failed: {e}")

    return None


def get_naive_bayes_prediction(structured_json: dict) -> dict[str, Any] | None:
    """Get Naive Bayes model prediction."""
    try:
        # Import from app module when running in Flask context
        try:
            from app.services.ml_claim_model import predict_claim_recommendation
            from app.db.session import SessionLocal

            db = SessionLocal()
            claim_text = " ".join(
                str(structured_json.get(field, ""))
                for field in [
                    "diagnosis",
                    "findings",
                    "investigation_finding_in_details",
                    "medicine_used",
                ]
            )

            prediction = predict_claim_recommendation(
                db=db,
                claim_text=claim_text,
                force_retrain=False,
            )
            db.close()

            if prediction.available and prediction.label:
                return {
                    "label": str(prediction.label).strip().lower(),
                    "confidence": float(prediction.confidence or 0.0),
                    "model_version": prediction.model_version,
                    "model": "naive_bayes",
                }
        except ImportError:
            # Fallback: not in Flask context
            logger.debug("ML model not available in this context")
            return None

    except Exception as e:
        logger.warning(f"Naive Bayes prediction failed: {e}")

    return None


def combine_predictions(
    xgboost_pred: dict[str, Any] | None,
    naive_bayes_pred: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Combine XGBoost and Naive Bayes predictions.

    Strategies:
    1. Both agree → Use prediction with combined confidence
    2. Small disagreement (<20%) → Use higher confidence model
    3. Large disagreement (>20%) → Flag as "need_more_evidence"
    4. One fails → Use the other
    5. Both fail → Default to "need_more_evidence"
    """

    # Handle missing models
    if not xgboost_pred and not naive_bayes_pred:
        logger.warning("Both models failed - defaulting to need_more_evidence")
        return {
            "recommendation": "need_more_evidence",
            "confidence": 0.0,
            "decision_source": "ensemble_both_failed",
            "models_used": [],
            "reasoning": "Both XGBoost and Naive Bayes predictions failed",
            "agreement": False,
        }

    if not xgboost_pred:
        logger.info("XGBoost unavailable, using Naive Bayes only")
        return {
            "recommendation": naive_bayes_pred["label"],
            "confidence": naive_bayes_pred["confidence"],
            "decision_source": "ensemble_xgboost_failed",
            "models_used": ["naive_bayes"],
            "reasoning": "XGBoost model unavailable, using Naive Bayes",
            "agreement": False,
        }

    if not naive_bayes_pred:
        logger.info("Naive Bayes unavailable, using XGBoost only")
        return {
            "recommendation": xgboost_pred["label"],
            "confidence": xgboost_pred["confidence"],
            "decision_source": "ensemble_nb_failed",
            "models_used": ["xgboost"],
            "reasoning": "Naive Bayes model unavailable, using XGBoost",
            "agreement": False,
        }

    # Both models available - combine predictions
    xgb_label = xgboost_pred["label"]
    xgb_conf = xgboost_pred["confidence"]
    nb_label = naive_bayes_pred["label"]
    nb_conf = naive_bayes_pred["confidence"]

    # Check if models agree
    models_agree = xgb_label == nb_label

    if models_agree:
        # Both agree: weighted average confidence
        final_confidence = (xgb_conf * XGBOOST_WEIGHT) + (nb_conf * NAIVE_BAYES_WEIGHT)

        logger.info(
            f"✅ Ensemble agreement: {xgb_label} "
            f"(XGB: {xgb_conf:.1%}, NB: {nb_conf:.1%}, combined: {final_confidence:.1%})"
        )

        return {
            "recommendation": xgb_label,
            "confidence": final_confidence,
            "decision_source": "ensemble_agreement",
            "models_used": ["xgboost", "naive_bayes"],
            "reasoning": (
                f"Both models agree: {xgb_label.upper()} "
                f"(XGBoost: {xgb_conf:.1%}, Naive Bayes: {nb_conf:.1%})"
            ),
            "agreement": True,
        }

    else:
        # Disagreement detected
        confidence_diff = abs(xgb_conf - nb_conf)
        higher_conf_model = "xgboost" if xgb_conf >= nb_conf else "naive_bayes"
        higher_conf_label = xgb_label if xgb_conf >= nb_conf else nb_label
        higher_conf_value = max(xgb_conf, nb_conf)

        if confidence_diff > DISAGREEMENT_THRESHOLD:
            # Large disagreement → escalate to manual review
            logger.warning(
                f"⚠️ Large model disagreement (diff={confidence_diff:.1%}): "
                f"XGB says {xgb_label} ({xgb_conf:.1%}), "
                f"NB says {nb_label} ({nb_conf:.1%})"
            )

            return {
                "recommendation": "need_more_evidence",
                "confidence": higher_conf_value * 0.75,  # Reduced confidence
                "decision_source": "ensemble_disagreement_large",
                "models_used": ["xgboost", "naive_bayes"],
                "reasoning": (
                    f"Models disagree significantly (diff={confidence_diff:.1%}): "
                    f"XGBoost predicts {xgb_label} ({xgb_conf:.1%}), "
                    f"Naive Bayes predicts {nb_label} ({nb_conf:.1%}). "
                    f"Escalating to manual review for safety."
                ),
                "agreement": False,
                "disagreement_margin": confidence_diff,
            }

        else:
            # Small disagreement → use higher confidence model
            logger.info(
                f"Minor model disagreement (diff={confidence_diff:.1%}): "
                f"using {higher_conf_model} prediction ({higher_conf_label})"
            )

            return {
                "recommendation": higher_conf_label,
                "confidence": higher_conf_value,
                "decision_source": f"ensemble_{higher_conf_model}_winner",
                "models_used": ["xgboost", "naive_bayes"],
                "reasoning": (
                    f"Minor disagreement (diff={confidence_diff:.1%}): "
                    f"using {higher_conf_model} ({higher_conf_label} @ {higher_conf_value:.1%})"
                ),
                "agreement": False,
                "disagreement_margin": confidence_diff,
            }


def predict_with_ensemble(structured_json: dict) -> dict[str, Any]:
    """
    Get ensemble prediction combining XGBoost and Naive Bayes.

    This is the main entry point for Stage 3 report generation.

    Returns:
        {
            "recommendation": "approve|reject|need_more_evidence",
            "confidence": float (0-1),
            "decision_source": str,
            "models_used": list,
            "reasoning": str,
            "agreement": bool,
        }
    """

    # Get predictions from both models
    xgboost_pred = get_xgboost_prediction(structured_json)
    naive_bayes_pred = get_naive_bayes_prediction(structured_json)

    # Combine using ensemble logic
    ensemble_result = combine_predictions(xgboost_pred, naive_bayes_pred)

    logger.info(
        f"📊 Ensemble Result: {ensemble_result['recommendation']} "
        f"(confidence: {ensemble_result['confidence']:.1%}, "
        f"source: {ensemble_result['decision_source']}, "
        f"models: {', '.join(ensemble_result['models_used']) or 'none'})"
    )

    return ensemble_result
