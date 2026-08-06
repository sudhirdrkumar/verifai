INSERT INTO rule_registry (rule_key, version, status, rule_definition, created_by)
VALUES (
    'default_medical_consistency',
    '1.0.0',
    'active',
    '{"description":"Baseline consistency checks for diagnosis-test-treatment alignment"}'::jsonb,
    'system'
)
ON CONFLICT (rule_key, version) DO NOTHING;

INSERT INTO model_registry (model_key, version, status, metrics, approved_by, approved_at)
VALUES (
    'fraud_risk_score',
    'shadow-1.0.0',
    'shadow',
    '{"auc":0.0,"note":"placeholder shadow model"}'::jsonb,
    'system',
    NOW()
)
ON CONFLICT (model_key, version) DO NOTHING;
