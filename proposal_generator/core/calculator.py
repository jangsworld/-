"""
Scoring calculation logic for the Quantitative Proposal Generator.
"""

GRADE_POINTS = {
    '특급': 4.0,
    '고급': 3.0,
    '중급': 2.0,
    '초급': 1.0,
}

# Default credit rating → score fraction mapping
# Key = rating string, Value = fraction of max_score (0.0 ~ 1.0)
DEFAULT_CREDIT_SCORING = {
    'AAA':  1.0,
    'AA+':  0.967,
    'AA':   0.933,
    'AA-':  0.9,
    'A+':   0.867,
    'A':    0.833,
    'A-':   0.8,
    'BBB+': 1.0,
    'BBB':  0.833,
    'BBB-': 0.667,
    'BB+':  0.5,
    'BB':   0.333,
    'BB-':  0.167,
    'B+':   0.1,
    'B':    0.067,
    'B-':   0.033,
    'CCC':  0.0,
    'CC':   0.0,
    'C':    0.0,
    'D':    0.0,
}

# Default experience thresholds: list of [min_percent, score_fraction]
# Sorted descending by min_percent
DEFAULT_EXPERIENCE_THRESHOLDS = [
    [100, 1.0],
    [80,  0.8],
    [60,  0.6],
    [40,  0.4],
    [20,  0.2],
]

# Default retention thresholds: [min_raw_score, score_fraction]
DEFAULT_RETENTION_THRESHOLDS = [
    [40, 1.0],
    [30, 0.8],
    [20, 0.6],
    [10, 0.4],
]

# Default deployment thresholds: [min_raw_score, score_fraction]
DEFAULT_DEPLOYMENT_THRESHOLDS = [
    [10, 1.0],
    [7,  0.7],
    [4,  0.4],
]


def calc_management_score(rating: str, max_score: float,
                           scoring_table: dict = None) -> float:
    """
    Calculate 경영실태 (신용평가등급) score.

    Args:
        rating: Credit rating string e.g. 'BBB+'
        max_score: Maximum possible score for this section
        scoring_table: dict mapping rating -> fraction (0.0~1.0), defaults to DEFAULT_CREDIT_SCORING

    Returns:
        Computed score (float)
    """
    if scoring_table is None:
        scoring_table = DEFAULT_CREDIT_SCORING
    fraction = scoring_table.get(rating, 0.0)
    score = round(fraction * max_score, 2)
    return score


def calc_experience_score(total_amount: int, bid_amount: int, max_score: float,
                          thresholds: list = None):
    """
    Calculate 수행경험 score.

    Args:
        total_amount: Sum of all selected project amounts (VAT inclusive)
        bid_amount: 사업비 from current bid (VAT inclusive)
        max_score: Maximum possible score for this section
        thresholds: list of [min_percent, score_fraction], sorted descending by min_percent

    Returns:
        (score: float, percentage: float)
    """
    if thresholds is None:
        thresholds = DEFAULT_EXPERIENCE_THRESHOLDS

    if bid_amount <= 0:
        return 0.0, 0.0

    percentage = (total_amount / bid_amount) * 100.0

    # Sort thresholds descending
    sorted_thresholds = sorted(thresholds, key=lambda x: x[0], reverse=True)

    fraction = 0.0
    for min_pct, frac in sorted_thresholds:
        if percentage >= min_pct:
            fraction = frac
            break

    score = round(fraction * max_score, 2)
    return score, round(percentage, 1)


def calc_technician_score(technicians_retention: list, technicians_deployment: list,
                          max_score: float,
                          retention_thresholds: list = None,
                          deployment_thresholds: list = None) -> dict:
    """
    Calculate 기술인력 score.

    Args:
        technicians_retention: list of technician dicts with 'grade' key
        technicians_deployment: list of technician dicts with 'grade' key
        max_score: Total max score for this section (split evenly 50/50)
        retention_thresholds: list of [min_raw, score_fraction]
        deployment_thresholds: list of [min_raw, score_fraction]

    Returns:
        dict with keys: retention_raw, deployment_raw, retention_score, deployment_score, total_score
    """
    if retention_thresholds is None:
        retention_thresholds = DEFAULT_RETENTION_THRESHOLDS
    if deployment_thresholds is None:
        deployment_thresholds = DEFAULT_DEPLOYMENT_THRESHOLDS

    half_max = max_score / 2.0

    # Compute raw scores
    retention_raw = sum(GRADE_POINTS.get(t.get('grade', '초급'), 1.0) for t in technicians_retention)
    deployment_raw = sum(GRADE_POINTS.get(t.get('grade', '초급'), 1.0) for t in technicians_deployment)

    def apply_thresholds(raw, thresholds, half_max_score):
        sorted_t = sorted(thresholds, key=lambda x: x[0], reverse=True)
        fraction = 0.0
        for min_raw, frac in sorted_t:
            if raw >= min_raw:
                fraction = frac
                break
        return round(fraction * half_max_score, 2)

    retention_score = apply_thresholds(retention_raw, retention_thresholds, half_max)
    deployment_score = apply_thresholds(deployment_raw, deployment_thresholds, half_max)
    total_score = round(retention_score + deployment_score, 2)

    return {
        'retention_raw': round(retention_raw, 1),
        'deployment_raw': round(deployment_raw, 1),
        'retention_score': retention_score,
        'deployment_score': deployment_score,
        'total_score': total_score,
    }


def calc_reputation_score(status: str, max_score: float) -> float:
    """
    Calculate 신인도 score.

    Args:
        status: 'none' (no restriction), 'restricted', or 'no_docs'
        max_score: Maximum possible score for this section

    Returns:
        Score (float)
    """
    if status == 'none':
        return round(max_score, 2)
    elif status in ('restricted', 'no_docs'):
        return 1.4
    return 0.0


def calc_total_score(mgmt: float, exp: float, tech: float, rep: float,
                     extra_items: list) -> float:
    """
    Calculate total score.

    Args:
        mgmt: 경영실태 score
        exp: 수행경험 score
        tech: 기술인력 total score
        rep: 신인도 score
        extra_items: list of dicts with 'actual_score' key

    Returns:
        Total score (float)
    """
    extra_total = sum(item.get('actual_score', 0.0) for item in extra_items)
    total = mgmt + exp + tech + rep + extra_total
    return round(total, 2)
