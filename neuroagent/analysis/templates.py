"""Safe, reviewable Python ML template generation."""

from __future__ import annotations

import hashlib
import json
import textwrap
from typing import Any

from neuroagent.analysis.models import MlDesignRecommendation, MlTemplate


def _content_hash(value: Any) -> str:
    """Deterministic hash kept local to avoid importing the application layer."""
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def generate_ml_template(design: MlDesignRecommendation, *, source_filename: str) -> MlTemplate:
    features = repr(list(design.feature_columns))
    models = repr([model.value for model in design.models])
    content = textwrap.dedent(
        f'''\
        """Generated rs-fMRI tabular ML template.

        This file is a reviewed starting point. It never reads an absolute path
        from a model response and must be run only after the design is approved.
        """
        from pathlib import Path
        import json
        import numpy as np
        import pandas as pd
        from sklearn.compose import ColumnTransformer
        from sklearn.impute import SimpleImputer
        from sklearn.metrics import (average_precision_score, balanced_accuracy_score,
                                     roc_auc_score, roc_curve)
        from sklearn.model_selection import StratifiedGroupKFold, cross_val_predict
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import OneHotEncoder, StandardScaler
        from sklearn.linear_model import LogisticRegression
        from sklearn.svm import SVC
        from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier

        SOURCE = Path({source_filename!r})
        TARGET = {design.target_column!r}
        GROUP = {design.group_column!r}
        FEATURES = {features}
        MODELS = {models}
        SEED = {design.seed}

        frame = pd.read_csv(SOURCE) if SOURCE.suffix.lower() != ".xlsx" else pd.read_excel(SOURCE)
        missing = sorted(set([TARGET, GROUP, *FEATURES]) - set(frame.columns))
        if missing:
            raise ValueError(f"Missing approved columns: {{missing}}")
        if frame[TARGET].isna().any() or frame[GROUP].isna().any():
            raise ValueError("Target and subject group columns cannot contain missing values")
        X = frame[FEATURES]
        y = frame[TARGET]
        groups = frame[GROUP]
        numeric = X.select_dtypes(include=["number"]).columns.tolist()
        categorical = [column for column in FEATURES if column not in numeric]
        preprocessor = ColumnTransformer([
            ("numeric", Pipeline([("impute", SimpleImputer(strategy="median")),
                                   ("scale", StandardScaler())]), numeric),
            ("categorical", Pipeline([
                ("impute", SimpleImputer(strategy="most_frequent")),
                ("encode", OneHotEncoder(handle_unknown="ignore")),
            ]), categorical),
        ])
        estimators = {{
            "logistic_regression": LogisticRegression(max_iter=2000, random_state=SEED),
            "svm": SVC(probability=True, random_state=SEED),
            "random_forest": RandomForestClassifier(n_estimators=300, random_state=SEED, n_jobs=1),
            "gradient_boosting": GradientBoostingClassifier(random_state=SEED),
        }}
        cv = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=SEED)
        results = []
        for name in MODELS:
            pipeline = Pipeline([("preprocess", preprocessor), ("model", estimators[name])])
            probabilities = cross_val_predict(
                pipeline, X, y, groups=groups, cv=cv, method="predict_proba"
            )[:, 1]
            results.append({{"model": name, "roc_auc": roc_auc_score(y, probabilities),
                            "average_precision": average_precision_score(y, probabilities)}})
        pd.DataFrame(results).to_csv("ml_metrics.csv", index=False)
        Path("ml_design.json").write_text(
            json.dumps({design.model_dump(mode="json")}, indent=2), encoding="utf-8"
        )
        print("Review generated metrics and subject-level split before scientific use.")
        '''
    )
    return MlTemplate(
        filename="rsfmri_ml_template.py",
        content=content,
        design_hash=_content_hash(design.model_dump(mode="json")),
    )
