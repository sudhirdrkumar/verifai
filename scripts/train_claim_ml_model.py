import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.db.session import SessionLocal
from app.services.ml_claim_model import ensure_model


def main() -> None:
    parser = argparse.ArgumentParser(description="Train claim recommendation ML model")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force retraining even if an active model exists",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        model = ensure_model(db=db, force_retrain=args.force)
    finally:
        db.close()

    if not isinstance(model, dict):
        print("ML model could not be trained. Need more labeled data.")
        raise SystemExit(1)

    print("ML model ready.")
    print(f"Model key: {model.get('model_key')}")
    print(f"Version: {model.get('version')}")
    print(f"Examples: {model.get('num_examples')}")
    print(f"Label counts: {model.get('label_counts')}")
    print(f"Vocab size: {len(model.get('vocab') or [])}")


if __name__ == "__main__":
    main()
