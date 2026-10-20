import re
import math
import pandas as pd
import numpy as np

# 집계 행 식별 키워드 목록
AGGREGATE_KEYWORDS = [
    "world", "oecd members", "oecd", "euro area", "european union",
    "high income", "low income", "middle income", "upper middle income", "lower middle income",
    "income", "area", "total", "average", "dividend", "small states", "ida & ibrd",
    "sub-saharan", "latin america & caribbean", "middle east & north africa", "south asia",
    "east asia & pacific", "europe & central asia", "north america"
]

CHART_COLORS = [
    "#3b82f6", "#ef4444", "#10b981", "#f59e0b", "#8b5cf6",
    "#06b6d4", "#ec4899", "#84cc16", "#64748b", "#f97316"
]


def clean_nan(obj):
    """
    NaN, Infinity, -Infinity 값을 JSON 직렬화가 가능한 None 또는 유효한 값으로 재귀 변환합니다.
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif isinstance(obj, (np.floating, np.integer)):
        val = obj.item()
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return None
        return val
    elif isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_nan(v) for v in obj]
    elif pd.isna(obj):
        return None
    return obj


def find_year_columns(df):
    """DataFrame에서 4자리 연도(1900~2099) 형태의 컬럼 목록을 반환합니다."""
    return [str(c) for c in df.columns if re.match(r"^(19|20)\d{2}$", str(c).strip())]


def find_label_column(df, explicit_col=None):
    """항목명/국가명으로 사용할 대표 라벨 컬럼을 탐색합니다."""
    if explicit_col and explicit_col in df.columns:
        return explicit_col
    for col in ["Country Name", "Country", "Name", "Entity", "Item", "Region"]:
        if col in df.columns:
            return col
    str_cols = df.select_dtypes(include=["object", "string"]).columns
    if len(str_cols) > 0:
        return str_cols[0]
    return df.columns[0] if len(df.columns) > 0 else None


def get_aggregate_mask(df, label_col=None):
    """집계 행(World, Income group 등)에 해당하는 불리언 마스크를 반환합니다."""
    l_col = find_label_column(df, label_col)
    if not l_col or l_col not in df.columns:
        return pd.Series(False, index=df.index)

    series_lower = df[l_col].astype(str).str.lower().str.strip()
    mask = pd.Series(False, index=df.index)
    for kw in AGGREGATE_KEYWORDS:
        mask = mask | series_lower.str.contains(kw, regex=False, na=False)
    return mask


# ==============================================================================
# 도구 1. reshape_wide_to_long (연도 열을 행으로 접기)
# ==============================================================================
def reshape_wide_to_long(df, id_vars=None, value_vars=None, var_name="Year", value_name="Value", exclude_aggregates=True):
    work_df = df.copy()
    label_col = find_label_column(work_df)
    total_original_rows = len(work_df)
    excluded_count = 0
    excluded_reason = "없음"

    if exclude_aggregates:
        agg_mask = get_aggregate_mask(work_df, label_col)
        excluded_count = int(agg_mask.sum())
        if excluded_count > 0:
            work_df = work_df[~agg_mask].copy()
            excluded_reason = f"지역·소득 그룹 집계 행 {excluded_count}개 제외"

    detected_years = find_year_columns(work_df)
    if value_vars is None:
        value_vars = detected_years

    if id_vars is None:
        id_vars = [
            c for c in work_df.columns 
            if c not in value_vars and not str(c).startswith("Unnamed:") and str(c).strip() != ""
        ]

    melted = pd.melt(
        work_df,
        id_vars=id_vars,
        value_vars=value_vars,
        var_name=var_name,
        value_name=value_name
    )

    non_null_melted = melted.dropna(subset=[value_name])
    preview_records = non_null_melted.head(15).to_dict(orient="records")

    result = {
        "tool": "reshape_wide_to_long",
        "reference_period": f"{value_vars[0]}~{value_vars[-1]}" if value_vars else "전체",
        "used_rows_count": int(len(work_df)),
        "excluded_rows_count": excluded_count,
        "excluded_reason": excluded_reason,
        "used_columns": id_vars + [var_name, value_name],
        "unit": "행 (롱 포맷)",
        "caution": "연도가 행으로 전환되어 총 행 수가 증가합니다. 분석 도구와 시각화 라이브러리에 바로 입력 가능한 형태입니다.",
        "summary": {
            "original_wide_shape": [total_original_rows, len(df.columns)],
            "long_format_total_rows": int(len(melted)),
            "long_format_valid_rows": int(len(non_null_melted)),
            "melted_columns": list(melted.columns)
        },
        "preview": clean_nan(preview_records)
    }
    return clean_nan(result)


# ==============================================================================
# 도구 2. quality_report (결측·중복·타입·이상 열 진단 보고서)
# ==============================================================================
def quality_report(df, exclude_aggregates=False):
    work_df = df.copy()
    label_col = find_label_column(work_df)
    total_original = len(work_df)
    excluded_count = 0
    excluded_reason = "없음"

    if exclude_aggregates:
        agg_mask = get_aggregate_mask(work_df, label_col)
        excluded_count = int(agg_mask.sum())
        if excluded_count > 0:
            work_df = work_df[~agg_mask].copy()
            excluded_reason = f"집계 행 {excluded_count}개 제외 후 진단"

    total_rows = len(work_df)
    columns_report = []

    for col in work_df.columns:
        scol = str(col)
        dtype = str(work_df[col].dtype)
        null_count = int(work_df[col].isna().sum())
        null_ratio = round(null_count / total_rows, 4) if total_rows > 0 else 0.0
        unique_count = int(work_df[col].nunique(dropna=True))

        columns_report.append({
            "column": scol,
            "dtype": dtype,
            "total_count": total_rows,
            "null_count": null_count,
            "null_ratio": null_ratio,
            "null_pct": round(null_ratio * 100, 1),
            "unique_count": unique_count,
            "is_unnamed": scol.startswith("Unnamed:") or scol.strip() == ""
        })

    duplicate_rows = int(work_df.duplicated().sum())
    unnamed_cols = [c["column"] for c in columns_report if c["is_unnamed"]]

    result = {
        "tool": "quality_report",
        "reference_period": "전체 데이터셋",
        "used_rows_count": total_rows,
        "excluded_rows_count": excluded_count,
        "excluded_reason": excluded_reason,
        "used_columns": [str(c) for c in work_df.columns],
        "unit": "항목 및 비율(%)",
        "caution": "결측치가 50% 이상인 열은 단독 분석 시 표본 편향이 발생할 수 있으므로 주의해야 합니다.",
        "summary": {
            "total_rows": total_rows,
            "total_columns": len(work_df.columns),
            "duplicate_rows": duplicate_rows,
            "unnamed_columns_count": len(unnamed_cols),
            "unnamed_columns": unnamed_cols
        },
        "columns_detail": columns_report
    }
    return clean_nan(result)


# ==============================================================================
# 도구 3. year_coverage (연도별 값 개수와 권장 기준연도 탐색)
# ==============================================================================
def year_coverage(df, exclude_aggregates=True):
    work_df = df.copy()
    label_col = find_label_column(work_df)
    excluded_count = 0
    excluded_reason = "없음"

    if exclude_aggregates:
        agg_mask = get_aggregate_mask(work_df, label_col)
        excluded_count = int(agg_mask.sum())
        if excluded_count > 0:
            work_df = work_df[~agg_mask].copy()
            excluded_reason = f"순수 개별 항목만 평가하기 위해 집계 행 {excluded_count}개 제외"

    total_rows = len(work_df)
    year_cols = find_year_columns(work_df)

    if not year_cols:
        return {
            "error": "연도 형태(예: 1960~2025)의 컬럼을 찾을 수 없습니다.",
            "tool": "year_coverage"
        }

    coverage_table = []
    labels = []
    counts = []

    for yc in year_cols:
        valid_cnt = int(work_df[yc].notna().sum())
        ratio = round(valid_cnt / total_rows, 4) if total_rows > 0 else 0.0
        pct = round(ratio * 100, 1)

        coverage_table.append({
            "year": yc,
            "filled_count": valid_cnt,
            "total_count": total_rows,
            "filled_ratio": ratio,
            "filled_pct": pct
        })
        labels.append(yc)
        counts.append(valid_cnt)

    rec_year = None
    for item in reversed(coverage_table):
        if item["filled_ratio"] >= 0.75:
            rec_year = item["year"]
            break
    if not rec_year:
        rec_year = max(coverage_table, key=lambda x: x["filled_ratio"])["year"]

    rec_info = next(item for item in coverage_table if item["year"] == rec_year)

    result = {
        "tool": "year_coverage",
        "reference_period": f"{year_cols[0]}~{year_cols[-1]}",
        "used_rows_count": total_rows,
        "excluded_rows_count": excluded_count,
        "excluded_reason": excluded_reason,
        "used_columns": year_cols,
        "unit": "개 항목 (충실도 %)",
        "caution": f"최신 연도일지라도 데이터 수집이 덜 끝난 경우가 많으므로 충실도가 높은 {rec_year}년({rec_info['filled_pct']}%) 기준 분석을 권장합니다.",
        "recommended_year": rec_year,
        "recommended_year_info": rec_info,
        "coverage_table": coverage_table,
        "chart_data": {
            "type": "bar",
            "labels": labels,
            "datasets": [
                {
                    "label": "데이터 입력 개수",
                    "data": counts,
                    "backgroundColor": "#3b82f6"
                }
            ]
        }
    }
    return clean_nan(result)


# ==============================================================================
# 도구 4. detect_aggregates (집계 행 후보 탐지)
# ==============================================================================
def detect_aggregates(df, label_column=None):
    work_df = df.copy()
    l_col = find_label_column(work_df, label_column)
    total_rows = len(work_df)

    if not l_col or l_col not in work_df.columns:
        return {"error": "라벨로 사용할 컬럼을 찾을 수 없습니다.", "tool": "detect_aggregates"}

    agg_mask = get_aggregate_mask(work_df, l_col)
    agg_df = work_df[agg_mask]
    pure_df = work_df[~agg_mask]

    detected_items = []
    for idx, row in agg_df.iterrows():
        val_str = str(row[l_col]).strip()
        matched_kw = next((kw for kw in AGGREGATE_KEYWORDS if kw in val_str.lower()), "unknown")
        detected_items.append({
            "row_index": int(idx),
            "label": val_str,
            "matched_keyword": matched_kw
        })

    result = {
        "tool": "detect_aggregates",
        "reference_period": "전체",
        "used_rows_count": total_rows,
        "excluded_rows_count": 0,
        "excluded_reason": "모든 행을 대상으로 집계 행 여부를 분류함",
        "used_columns": [l_col],
        "unit": "개 행",
        "caution": "집계 행이 포함된 상태로 평균이나 합계를 계산하면 개별 항목이 이중 계산(Double-counting)되므로 반드시 제외해야 합니다.",
        "summary": {
            "total_rows": total_rows,
            "aggregate_rows_count": int(len(agg_df)),
            "pure_items_count": int(len(pure_df)),
            "aggregate_ratio_pct": round(len(agg_df) / total_rows * 100, 1) if total_rows > 0 else 0.0
        },
        "aggregate_items": detected_items,
        "chart_data": {
            "type": "doughnut",
            "labels": ["순수 개별 항목", "지역·소득 집계 행"],
            "datasets": [
                {
                    "data": [int(len(pure_df)), int(len(agg_df))],
                    "backgroundColor": ["#10b981", "#f59e0b"]
                }
            ]
        }
    }
    return clean_nan(result)


# ==============================================================================
# 도구 5. describe_numeric (개수·평균·중앙값·최소·최대·표준편차 기술통계)
# ==============================================================================
def describe_numeric(df, column=None, reference_period=None, exclude_aggregates=True):
    work_df = df.copy()
    label_col = find_label_column(work_df)
    total_original = len(work_df)
    excluded_reasons = []

    target_col = column if column else reference_period
    if not target_col:
        year_cols = find_year_columns(work_df)
        if year_cols:
            target_col = year_cols[-2] if len(year_cols) >= 2 else year_cols[-1]
        else:
            num_cols = work_df.select_dtypes(include=["number"]).columns
            if len(num_cols) > 0:
                target_col = num_cols[0]

    if not target_col or target_col not in work_df.columns:
        return {"error": f"분석할 숫자 컬럼 '{target_col}'을 찾을 수 없습니다.", "tool": "describe_numeric"}

    if exclude_aggregates:
        agg_mask = get_aggregate_mask(work_df, label_col)
        agg_excluded = int(agg_mask.sum())
        if agg_excluded > 0:
            work_df = work_df[~agg_mask].copy()
            excluded_reasons.append(f"집계 행 {agg_excluded}개 제외")

    series = pd.to_numeric(work_df[target_col], errors="coerce").dropna()
    null_excluded = len(work_df) - len(series)
    if null_excluded > 0:
        excluded_reasons.append(f"해당 시점 결측치(빈값) {null_excluded}개 제외")

    used_rows = len(series)
    total_excluded = total_original - used_rows
    excluded_reason = ", ".join(excluded_reasons) if excluded_reasons else "없음"

    if used_rows == 0:
        return {"error": f"'{target_col}' 컬럼에 유효한 숫자 데이터가 없습니다.", "tool": "describe_numeric"}

    cnt = int(series.count())
    mean_val = round(float(series.mean()), 2)
    std_val = round(float(series.std()), 2) if cnt > 1 else 0.0
    min_val = round(float(series.min()), 2)
    q25_val = round(float(series.quantile(0.25)), 2)
    median_val = round(float(series.median()), 2)
    q75_val = round(float(series.quantile(0.75)), 2)
    max_val = round(float(series.max()), 2)

    result = {
        "tool": "describe_numeric",
        "reference_period": str(target_col),
        "used_rows_count": used_rows,
        "excluded_rows_count": total_excluded,
        "excluded_reason": excluded_reason,
        "used_columns": [str(target_col)],
        "unit": "수치 (%)",
        "caution": "평균값은 소수의 극단적인 이상치(극단값)에 영향을 크게 받을 수 있으므로 중앙값과 함께 비교해야 합니다.",
        "statistics": {
            "count": cnt,
            "mean": mean_val,
            "std": std_val,
            "min": min_val,
            "q25": q25_val,
            "median": median_val,
            "q75": q75_val,
            "max": max_val
        }
    }
    return clean_nan(result)


# ==============================================================================
# 도구 6. trend_line (선택 항목들의 연도별 시계열 추이 선 차트)
# ==============================================================================
def trend_line(df, items=None, label_column=None, year_columns=None, exclude_aggregates=False):
    work_df = df.copy()
    l_col = find_label_column(work_df, label_column)

    if not l_col or l_col not in work_df.columns:
        return {"error": "라벨로 사용할 컬럼을 찾을 수 없습니다.", "tool": "trend_line"}

    if year_columns is None:
        year_columns = find_year_columns(work_df)

    if not year_columns:
        return {"error": "시계열을 구성할 연도 컬럼이 없습니다.", "tool": "trend_line"}

    if items is None or len(items) == 0:
        candidates = ["Korea, Rep.", "United States", "World", "China", "Japan", "Germany"]
        found = [c for c in candidates if c in work_df[l_col].values]
        if found:
            items = found[:4]
        else:
            items = list(work_df[l_col].dropna().unique()[:4])

    filtered_df = work_df[work_df[l_col].isin(items)].copy()
    used_rows = len(filtered_df)
    excluded_rows = len(df) - used_rows
    excluded_reason = f"선택한 {len(items)}개 항목 외 나머지 {excluded_rows}개 행 제외"

    datasets = []
    table_rows = []

    for i, item_name in enumerate(items):
        item_rows = filtered_df[filtered_df[l_col] == item_name]
        if item_rows.empty:
            continue
        row = item_rows.iloc[0]
        data_points = []
        row_dict = {"item": item_name}
        for yc in year_columns:
            val = pd.to_numeric(row.get(yc), errors="coerce")
            val_clean = round(float(val), 2) if pd.notna(val) else None
            data_points.append(val_clean)
            row_dict[yc] = val_clean
        table_rows.append(row_dict)

        color = CHART_COLORS[i % len(CHART_COLORS)]
        datasets.append({
            "label": str(item_name),
            "data": data_points,
            "borderColor": color,
            "backgroundColor": color,
            "fill": False,
            "tension": 0.2
        })

    result = {
        "tool": "trend_line",
        "reference_period": f"{year_columns[0]}~{year_columns[-1]}",
        "used_rows_count": used_rows,
        "excluded_rows_count": excluded_rows,
        "excluded_reason": excluded_reason,
        "used_columns": [l_col] + year_columns,
        "unit": "수치 (%)",
        "caution": "연도별 결측치가 존재하는 시점은 선이 끊기거나 보간될 수 있습니다.",
        "items": items,
        "table": table_rows,
        "chart_data": {
            "type": "line",
            "labels": year_columns,
            "datasets": datasets
        }
    }
    return clean_nan(result)


# ==============================================================================
# 도구 7. top_bottom_n (기준연도 기준 상위·하위 N 순위 분석 - 기준시점 필수!)
# ==============================================================================
def top_bottom_n(df, reference_period=None, n=10, label_column=None, exclude_aggregates=True, highlight_item=None, mode="top"):
    if not reference_period:
        return {
            "error": "기준 시점(reference_period, 예: '2024')을 반드시 입력해야 합니다.",
            "tool": "top_bottom_n"
        }

    ref_col = str(reference_period).strip()
    if ref_col not in df.columns:
        return {
            "error": f"데이터셋에 기준 시점 열 '{ref_col}'이 존재하지 않습니다.",
            "tool": "top_bottom_n"
        }

    work_df = df.copy()
    l_col = find_label_column(work_df, label_column)
    total_original = len(work_df)
    excluded_reasons = []

    if exclude_aggregates:
        agg_mask = get_aggregate_mask(work_df, l_col)
        agg_cnt = int(agg_mask.sum())
        if agg_cnt > 0:
            work_df = work_df[~agg_mask].copy()
            excluded_reasons.append(f"지역·소득 집계 행 {agg_cnt}개 제외")

    work_df["_numeric_val"] = pd.to_numeric(work_df[ref_col], errors="coerce")
    valid_df = work_df.dropna(subset=["_numeric_val"]).copy()
    null_cnt = len(work_df) - len(valid_df)
    if null_cnt > 0:
        excluded_reasons.append(f"{ref_col}년 데이터가 없는 결측 행 {null_cnt}개 제외")

    used_rows = len(valid_df)
    total_excluded = total_original - used_rows
    excluded_reason = ", ".join(excluded_reasons) if excluded_reasons else "없음"

    if used_rows == 0:
        return {"error": f"'{ref_col}' 연도에 유효한 데이터가 전혀 없습니다.", "tool": "top_bottom_n"}

    sorted_df = valid_df.sort_values(by="_numeric_val", ascending=False).reset_index(drop=True)
    n_count = min(int(n), len(sorted_df))

    top_df = sorted_df.head(n_count)
    bottom_df = sorted_df.tail(n_count).iloc[::-1]

    # 특정 강조/비교 대상 항목 찾기 (예: "Korea, Rep." 또는 한국)
    highlight_row = None
    highlight_rank = None
    if highlight_item:
        h_str = str(highlight_item).strip().lower()
        exact_match = sorted_df[sorted_df[l_col].astype(str).str.lower() == h_str]
        if not exact_match.empty:
            highlight_rank = int(exact_match.index[0]) + 1
            highlight_row = exact_match.iloc[0]
        else:
            search_term = "korea" if any(k in h_str for k in ["우리나라", "대한민국", "한국", "korea"]) else h_str
            partial_match = sorted_df[sorted_df[l_col].astype(str).str.lower().str.contains(search_term, na=False)]
            if not partial_match.empty:
                highlight_rank = int(partial_match.index[0]) + 1
                highlight_row = partial_match.iloc[0]

    top_records = []
    top_chart_labels = []
    top_chart_values = []
    top_chart_colors = []

    for rank, (idx, row) in enumerate(top_df.iterrows(), 1):
        item_label = str(row[l_col])
        item_val = round(float(row["_numeric_val"]), 2)
        is_hl = (highlight_row is not None and item_label == str(highlight_row[l_col]))

        display_label = f"🏆 [상위 {rank}위] {item_label}" + (" (🇰🇷 대한민국)" if is_hl else "")
        top_records.append({
            "rank": rank,
            "label": display_label,
            "raw_label": item_label,
            "value": item_val
        })
        top_chart_labels.append(display_label)
        top_chart_values.append(item_val)
        top_chart_colors.append("#ef4444" if is_hl else "#3b82f6")

    # 대한민국 비교 항목 (상위 N위 바깥인 경우)
    hl_label_display = None
    hl_val = None
    if highlight_row is not None and highlight_rank > n_count:
        hl_label = str(highlight_row[l_col])
        hl_val = round(float(highlight_row["_numeric_val"]), 2)
        hl_label_display = f"🇰🇷 [비교 {highlight_rank}위] {hl_label}"
        top_records.append({
            "rank": highlight_rank,
            "label": f"{hl_label} (🇰🇷 {highlight_rank}위 비교 대상)",
            "raw_label": hl_label,
            "value": hl_val
        })

    # 하위권 N개국 추출
    bottom_records = []
    bottom_chart_labels = []
    bottom_chart_values = []
    bottom_chart_colors = []

    for rank, (idx, row) in enumerate(bottom_df.iterrows(), 1):
        item_label = str(row[l_col])
        item_val = round(float(row["_numeric_val"]), 2)
        global_rank = int(idx) + 1
        display_label = f"🔻 [최하위 {rank}위 / {global_rank}위] {item_label}"
        bottom_records.append({
            "rank": rank,
            "global_rank": global_rank,
            "label": display_label,
            "raw_label": item_label,
            "value": item_val
        })
        bottom_chart_labels.append(display_label)
        bottom_chart_values.append(item_val)
        bottom_chart_colors.append("#f97316")

    # 모드에 따른 차트 데이터 및 테이블 결합
    mode_clean = str(mode).lower().strip()
    if mode_clean in ["neighbor", "below", "above", "around"]:
        center_rank = highlight_rank if highlight_rank is not None else 1
        center_idx = center_rank - 1

        if mode_clean == "below":
            # 기준 국가 1단계 위(있다면)부터 아래로 n개국 (총 n+2개국 안팎)
            start_i = max(0, center_idx - 1)
            end_i = min(len(sorted_df), center_idx + int(n) + 1)
        elif mode_clean == "above":
            start_i = max(0, center_idx - int(n))
            end_i = min(len(sorted_df), center_idx + 2)
        else: # neighbor, around
            span = max(3, int(n) // 2)
            start_i = max(0, center_idx - span)
            end_i = min(len(sorted_df), center_idx + span + 1)

        neighbor_df = sorted_df.iloc[start_i:end_i]
        chart_labels = []
        chart_values = []
        chart_colors = []
        combined_records = []

        for idx, row in neighbor_df.iterrows():
            cur_rank = int(idx) + 1
            item_label = str(row[l_col])
            item_val = round(float(row["_numeric_val"]), 2)

            if cur_rank == center_rank:
                display_label = f"🇰🇷 [{cur_rank}위] {item_label} (대한민국)"
                color = "#ef4444"
                category = "🇰🇷 기준 국가 (대한민국)"
            elif cur_rank == center_rank + 1:
                display_label = f"🔻 [바로 아래 {cur_rank}위] {item_label}"
                color = "#f97316"
                category = "🔻 바로 아래 순위"
            elif cur_rank == center_rank - 1:
                display_label = f"🔺 [바로 위 {cur_rank}위] {item_label}"
                color = "#10b981"
                category = "🔺 바로 위 순위"
            elif cur_rank > center_rank:
                display_label = f"[{cur_rank}위] {item_label}"
                color = "#fb923c"
                category = "하위 인접국"
            else:
                display_label = f"[{cur_rank}위] {item_label}"
                color = "#3b82f6"
                category = "상위 인접국"

            chart_labels.append(display_label)
            chart_values.append(item_val)
            chart_colors.append(color)
            combined_records.append({
                "구분": category,
                "순위": f"{cur_rank}위",
                "국가/항목명": item_label,
                "보급률(%)": item_val
            })

        chart_title = f"{ref_col}년 대한민국({center_rank}위) 및 인접·바로 아래 순위 비교"
    elif mode_clean in ["both", "top_and_bottom"]:
        chart_labels = top_chart_labels + ([hl_label_display] if hl_label_display else []) + bottom_chart_labels
        chart_values = top_chart_values + ([hl_val] if hl_val is not None else []) + bottom_chart_values
        chart_colors = top_chart_colors + (["#ef4444"] if hl_label_display else []) + bottom_chart_colors
        chart_title = f"{ref_col}년 순위 비교 (상위 {n_count}개국" + (" + 대한민국" if highlight_row is not None else "") + f" + 하위 {n_count}개국)"
    elif mode_clean == "bottom":
        chart_labels = bottom_chart_labels
        chart_values = bottom_chart_values
        chart_colors = bottom_chart_colors
        chart_title = f"{ref_col}년 하위 {n_count}개국 순위"
    else:
        chart_labels = top_chart_labels + ([hl_label_display] if hl_label_display else [])
        chart_values = top_chart_values + ([hl_val] if hl_val is not None else [])
        chart_colors = top_chart_colors + (["#ef4444"] if hl_label_display else [])
        chart_title = f"{ref_col}년 상위 {n_count}개국 순위" + (" + 대한민국" if highlight_row is not None else "")

    if mode_clean not in ["neighbor", "below", "above", "around"]:
        combined_records = []
        if mode_clean in ["top", "both", "top_and_bottom"]:
            for r in top_records:
                combined_records.append({
                    "구분": "🏆 상위권" if r["rank"] <= n_count else "🇰🇷 비교 대상",
                    "순위": f"{r['rank']}위",
                    "국가/항목명": r['raw_label'],
                    "보급률(%)": r['value']
                })
        if mode_clean in ["bottom", "both", "top_and_bottom"]:
            for r in bottom_records:
                combined_records.append({
                    "구분": "🔻 하위권",
                    "순위": f"최하위 {r['rank']}위 ({r['global_rank']}위)",
                    "국가/항목명": r['raw_label'],
                    "보급률(%)": r['value']
                })

    result = {
        "tool": "top_bottom_n",
        "reference_period": ref_col,
        "n": n_count,
        "mode": mode_clean,
        "highlight_item": str(highlight_row[l_col]) if highlight_row is not None else None,
        "highlight_rank": highlight_rank,
        "used_rows_count": used_rows,
        "excluded_rows_count": total_excluded,
        "excluded_reason": excluded_reason,
        "used_columns": [l_col, ref_col],
        "unit": "%",
        "caution": f"{ref_col}년에 값이 등록되지 않은 항목은 등수 집계에서 제외되었습니다.",
        "top_n": top_records,
        "bottom_n": bottom_records,
        "combined_n": combined_records,
        "chart_data": {
            "type": "bar",
            "indexAxis": "y",
            "labels": chart_labels,
            "datasets": [
                {
                    "label": chart_title,
                    "data": chart_values,
                    "backgroundColor": chart_colors if chart_colors else "#3b82f6"
                }
            ]
        }
    }
    return clean_nan(result)


# ==============================================================================
# 도구 8. group_summary (그룹별 평균·합계·개수 비교 - 기준시점 필수!)
# ==============================================================================
def group_summary(df, reference_period=None, group_column=None, value_column=None, exclude_aggregates=False):
    if not reference_period and not value_column:
        return {
            "error": "기준 시점(reference_period, 예: '2024')을 반드시 입력해야 합니다.",
            "tool": "group_summary"
        }

    v_col = str(value_column if value_column else reference_period).strip()
    if v_col not in df.columns:
        return {"error": f"분석할 값 컬럼 '{v_col}'이 존재하지 않습니다.", "tool": "group_summary"}

    work_df = df.copy()
    label_col = find_label_column(work_df)
    total_original = len(work_df)
    excluded_reasons = []

    if exclude_aggregates:
        agg_mask = get_aggregate_mask(work_df, label_col)
        agg_cnt = int(agg_mask.sum())
        if agg_cnt > 0:
            work_df = work_df[~agg_mask].copy()
            excluded_reasons.append(f"집계 행 {agg_cnt}개 제외")

    work_df["_num_val"] = pd.to_numeric(work_df[v_col], errors="coerce")
    valid_df = work_df.dropna(subset=["_num_val"]).copy()
    null_cnt = len(work_df) - len(valid_df)
    if null_cnt > 0:
        excluded_reasons.append(f"결측치 {null_cnt}개 제외")

    used_rows = len(valid_df)
    total_excluded = total_original - used_rows
    excluded_reason = ", ".join(excluded_reasons) if excluded_reasons else "없음"

    if used_rows == 0:
        return {"error": f"'{v_col}' 컬럼에 유효한 데이터가 없습니다.", "tool": "group_summary"}

    g_col = group_column
    if g_col and g_col in valid_df.columns:
        grouped = valid_df.groupby(g_col)["_num_val"]
    else:
        bins = [-np.inf, 30.0, 60.0, 85.0, np.inf]
        bin_labels = ["저보급(30% 미만)", "중보급(30~60%)", "고보급(60~85%)", "초고보급(85% 이상)"]
        valid_df["_auto_group"] = pd.cut(valid_df["_num_val"], bins=bins, labels=bin_labels)
        g_col = "보급률 구간(자동 그룹)"
        valid_df[g_col] = valid_df["_auto_group"]
        grouped = valid_df.groupby(g_col, observed=False)["_num_val"]

    summary_records = []
    labels = []
    means = []

    for grp_name, group_data in grouped:
        grp_cnt = int(group_data.count())
        grp_mean = round(float(group_data.mean()), 2) if grp_cnt > 0 else 0.0
        grp_median = round(float(group_data.median()), 2) if grp_cnt > 0 else 0.0
        grp_sum = round(float(group_data.sum()), 2) if grp_cnt > 0 else 0.0

        summary_records.append({
            "group": str(grp_name),
            "count": grp_cnt,
            "mean": grp_mean,
            "median": grp_median,
            "sum": grp_sum
        })
        labels.append(str(grp_name))
        means.append(grp_mean)

    result = {
        "tool": "group_summary",
        "reference_period": v_col,
        "used_rows_count": used_rows,
        "excluded_rows_count": total_excluded,
        "excluded_reason": excluded_reason,
        "used_columns": [g_col, v_col],
        "unit": "평균 수치 (%) 및 개수",
        "caution": "그룹별 항목 수의 편차가 큰 경우 평균값 비교 시 해석에 주의가 필요합니다.",
        "group_column": g_col,
        "summary_table": summary_records,
        "chart_data": {
            "type": "bar",
            "labels": labels,
            "datasets": [
                {
                    "label": "그룹별 평균 수치",
                    "data": means,
                    "backgroundColor": "#3b82f6"
                }
            ]
        }
    }
    return clean_nan(result)


# ==============================================================================
# 도구 9. change_rate (두 시점 사이 변화량·변화율 - 기준시점 필수!)
# ==============================================================================
def change_rate(df, reference_period=None, base_period=None, n=10, label_column=None, exclude_aggregates=True):
    if not reference_period:
        return {
            "error": "기준 시점(reference_period, 예: '2024')을 반드시 입력해야 합니다.",
            "tool": "change_rate"
        }

    ref_col = str(reference_period).strip()
    if ref_col not in df.columns:
        return {"error": f"데이터셋에 기준 시점 열 '{ref_col}'이 존재하지 않습니다.", "tool": "change_rate"}

    # 비교 기준 시점 자동 선택 (기본 10년 전 또는 5년 전)
    work_df = df.copy()
    l_col = find_label_column(work_df, label_column)
    year_cols = find_year_columns(work_df)

    if not base_period:
        if ref_col.isdigit() and str(int(ref_col) - 10) in df.columns:
            base_period = str(int(ref_col) - 10)
        elif ref_col.isdigit() and str(int(ref_col) - 5) in df.columns:
            base_period = str(int(ref_col) - 5)
        elif year_cols and year_cols[0] != ref_col:
            base_period = year_cols[0]
        else:
            return {"error": "비교할 이전 시점(base_period)을 지정할 수 없습니다.", "tool": "change_rate"}

    base_col = str(base_period).strip()
    if base_col not in df.columns:
        return {"error": f"비교 시점 열 '{base_col}'이 데이터셋에 없습니다.", "tool": "change_rate"}

    total_original = len(work_df)
    excluded_reasons = []

    if exclude_aggregates:
        agg_mask = get_aggregate_mask(work_df, l_col)
        agg_cnt = int(agg_mask.sum())
        if agg_cnt > 0:
            work_df = work_df[~agg_mask].copy()
            excluded_reasons.append(f"지역·소득 집계 행 {agg_cnt}개 제외")

    s_base = pd.to_numeric(work_df[base_col], errors="coerce")
    s_ref = pd.to_numeric(work_df[ref_col], errors="coerce")

    valid_mask = s_base.notna() & s_ref.notna()
    valid_df = work_df[valid_mask].copy()
    valid_df["_base_val"] = s_base[valid_mask]
    valid_df["_ref_val"] = s_ref[valid_mask]

    null_cnt = len(work_df) - len(valid_df)
    if null_cnt > 0:
        excluded_reasons.append(f"두 시점 중 하나라도 값이 없는 행 {null_cnt}개 제외")

    used_rows = len(valid_df)
    total_excluded = total_original - used_rows
    excluded_reason = ", ".join(excluded_reasons) if excluded_reasons else "없음"

    if used_rows == 0:
        return {"error": f"'{base_col}'과 '{ref_col}' 사이에 비교 가능한 유효 데이터가 없습니다.", "tool": "change_rate"}

    # 절대 변화량(Difference) 및 상대 변화율(Percentage Change)
    valid_df["_diff"] = valid_df["_ref_val"] - valid_df["_base_val"]
    valid_df["_pct_change"] = np.where(
        valid_df["_base_val"] > 0,
        ((valid_df["_diff"] / valid_df["_base_val"]) * 100),
        0.0
    )

    # 상승폭 기준 상위 N개
    top_gainers_df = valid_df.sort_values(by="_diff", ascending=False).head(int(n))

    table_records = []
    labels = []
    diffs = []

    for rank, (idx, row) in enumerate(top_gainers_df.iterrows(), 1):
        item_lbl = str(row[l_col])
        d_val = round(float(row["_diff"]), 2)
        table_records.append({
            "rank": rank,
            "label": item_lbl,
            "base_value": round(float(row["_base_val"]), 2),
            "ref_value": round(float(row["_ref_val"]), 2),
            "absolute_change": d_val,
            "percentage_change": round(float(row["_pct_change"]), 2)
        })
        labels.append(item_lbl)
        diffs.append(d_val)

    result = {
        "tool": "change_rate",
        "reference_period": f"{base_col}년 -> {ref_col}년",
        "used_rows_count": used_rows,
        "excluded_rows_count": total_excluded,
        "excluded_reason": excluded_reason,
        "used_columns": [l_col, base_col, ref_col],
        "unit": "%p (퍼센트포인트 증가량)",
        "caution": "초기 보급률이 매우 낮았던 국가는 작은 수치 증가로도 변화율(%)이 비정상적으로 높게 나타날 수 있으므로 절대 변화량(%p)을 함께 봐야 합니다.",
        "base_period": base_col,
        "reference_target_period": ref_col,
        "top_gainers": table_records,
        "chart_data": {
            "type": "bar",
            "labels": labels,
            "datasets": [
                {
                    "label": f"{base_col} 대비 {ref_col} 증가량 (%p)",
                    "data": diffs,
                    "backgroundColor": "#10b981"
                }
            ]
        }
    }
    return clean_nan(result)


# ==============================================================================
# 도구 10. distribution (구간별 빈도 분포 히스토그램 - 기준시점 필수!)
# ==============================================================================
def distribution(df, reference_period=None, bins_count=10, exclude_aggregates=True):
    if not reference_period:
        return {
            "error": "기준 시점(reference_period, 예: '2024')을 반드시 입력해야 합니다.",
            "tool": "distribution"
        }

    ref_col = str(reference_period).strip()
    if ref_col not in df.columns:
        return {"error": f"데이터셋에 기준 시점 열 '{ref_col}'이 존재하지 않습니다.", "tool": "distribution"}

    work_df = df.copy()
    label_col = find_label_column(work_df)
    total_original = len(work_df)
    excluded_reasons = []

    if exclude_aggregates:
        agg_mask = get_aggregate_mask(work_df, label_col)
        agg_cnt = int(agg_mask.sum())
        if agg_cnt > 0:
            work_df = work_df[~agg_mask].copy()
            excluded_reasons.append(f"지역·소득 집계 행 {agg_cnt}개 제외")

    series = pd.to_numeric(work_df[ref_col], errors="coerce").dropna()
    null_cnt = len(work_df) - len(series)
    if null_cnt > 0:
        excluded_reasons.append(f"결측 행 {null_cnt}개 제외")

    used_rows = len(series)
    total_excluded = total_original - used_rows
    excluded_reason = ", ".join(excluded_reasons) if excluded_reasons else "없음"

    if used_rows == 0:
        return {"error": f"'{ref_col}' 컬럼에 유효한 숫자 데이터가 없습니다.", "tool": "distribution"}

    # 히스토그램 빈 계산
    counts, bin_edges = np.histogram(series, bins=int(bins_count))
    bin_labels = []
    bins_detail = []

    for i in range(len(counts)):
        low = round(float(bin_edges[i]), 1)
        high = round(float(bin_edges[i + 1]), 1)
        lbl = f"{low}~{high}%"
        cnt = int(counts[i])
        pct = round(cnt / used_rows * 100, 1) if used_rows > 0 else 0.0
        bin_labels.append(lbl)
        bins_detail.append({
            "interval": lbl,
            "range_min": low,
            "range_max": high,
            "count": cnt,
            "percentage": pct
        })

    result = {
        "tool": "distribution",
        "reference_period": ref_col,
        "used_rows_count": used_rows,
        "excluded_rows_count": total_excluded,
        "excluded_reason": excluded_reason,
        "used_columns": [ref_col],
        "unit": "국가(항목) 수 및 비율(%)",
        "caution": "특정 구간(예: 90% 이상 고보급)에 데이터가 집중되는 편향(Skewness)이 나타날 수 있습니다.",
        "bins_summary": bins_detail,
        "chart_data": {
            "type": "bar",
            "labels": bin_labels,
            "datasets": [
                {
                    "label": f"{ref_col}년 보급률 구간별 국가 수",
                    "data": [int(c) for c in counts],
                    "backgroundColor": "#6366f1"
                }
            ]
        }
    }
    return clean_nan(result)


# ==============================================================================
# 도구 11. correlation_scatter (두 숫자 열 상관계수 및 산점도 - 기준시점 필수!)
# ==============================================================================
def correlation_scatter(df, reference_period=None, col_x=None, col_y=None, label_column=None, exclude_aggregates=True):
    if not reference_period and not (col_x and col_y):
        return {
            "error": "기준 시점(reference_period, 예: '2024')을 반드시 입력해야 합니다.",
            "tool": "correlation_scatter"
        }

    work_df = df.copy()
    l_col = find_label_column(work_df, label_column)
    year_cols = find_year_columns(work_df)

    # col_y와 col_x 기본 설정
    if not col_y:
        col_y = str(reference_period).strip()

    if not col_x:
        # 10년 전 연도 또는 직전 연도
        if col_y.isdigit() and str(int(col_y) - 10) in df.columns:
            col_x = str(int(col_y) - 10)
        elif year_cols and len(year_cols) >= 2:
            col_x = year_cols[0] if year_cols[0] != col_y else year_cols[1]
        else:
            num_cols = [c for c in work_df.select_dtypes(include=["number"]).columns if c != col_y]
            col_x = num_cols[0] if num_cols else None

    if not col_x or not col_y or col_x not in df.columns or col_y not in df.columns:
        return {"error": f"상관분석을 위한 두 컬럼('{col_x}', '{col_y}')을 찾을 수 없습니다.", "tool": "correlation_scatter"}

    total_original = len(work_df)
    excluded_reasons = []

    if exclude_aggregates:
        agg_mask = get_aggregate_mask(work_df, l_col)
        agg_cnt = int(agg_mask.sum())
        if agg_cnt > 0:
            work_df = work_df[~agg_mask].copy()
            excluded_reasons.append(f"지역·소득 집계 행 {agg_cnt}개 제외")

    s_x = pd.to_numeric(work_df[col_x], errors="coerce")
    s_y = pd.to_numeric(work_df[col_y], errors="coerce")

    valid_mask = s_x.notna() & s_y.notna()
    valid_df = work_df[valid_mask].copy()
    valid_df["_x"] = s_x[valid_mask]
    valid_df["_y"] = s_y[valid_mask]

    null_cnt = len(work_df) - len(valid_df)
    if null_cnt > 0:
        excluded_reasons.append(f"두 변수 중 하나라도 결측치인 행 {null_cnt}개 제외")

    used_rows = len(valid_df)
    total_excluded = total_original - used_rows
    excluded_reason = ", ".join(excluded_reasons) if excluded_reasons else "없음"

    if used_rows < 2:
        return {"error": "상관계수를 산출할 유효한 데이터 쌍(최소 2개 이상)이 부족합니다.", "tool": "correlation_scatter"}

    # 피어슨 상관계수 r 계산
    corr_r = round(float(valid_df["_x"].corr(valid_df["_y"])), 4)

    # 산점도 데이터 포인트 (최대 100개 샘플링)
    scatter_points = []
    for idx, row in valid_df.iterrows():
        scatter_points.append({
            "x": round(float(row["_x"]), 2),
            "y": round(float(row["_y"]), 2),
            "label": str(row[l_col])
        })

    result = {
        "tool": "correlation_scatter",
        "reference_period": f"{col_x} vs {col_y}",
        "used_rows_count": used_rows,
        "excluded_rows_count": total_excluded,
        "excluded_reason": excluded_reason,
        "used_columns": [col_x, col_y],
        "unit": "피어슨 상관계수 (r)",
        # 중요 규칙: 상관관계는 인과관계가 아니다 문구 필수 포함
        "caution": "상관관계는 인과관계가 아닙니다. 두 변수가 밀접한 상관관계를 보인다 하더라도 한쪽이 다른 쪽의 원인이라고 단정할 수 없습니다.",
        "correlation_coefficient_r": corr_r,
        "col_x": col_x,
        "col_y": col_y,
        "scatter_points": scatter_points,
        "chart_data": {
            "type": "scatter",
            "datasets": [
                {
                    "label": f"{col_x} vs {col_y} 산점도 (r = {corr_r})",
                    "data": [{"x": p["x"], "y": p["y"]} for p in scatter_points],
                    "backgroundColor": "#3b82f6"
                }
            ]
        }
    }
    return clean_nan(result)


# ==============================================================================
# 도구 12. compare_one_vs_all (특정 항목 vs 전체 평균·중앙값 강조 비교 - 기준시점 필수!)
# ==============================================================================
def compare_one_vs_all(df, reference_period=None, target_item="Korea, Rep.", label_column=None, exclude_aggregates=True):
    if not reference_period:
        return {
            "error": "기준 시점(reference_period, 예: '2024')을 반드시 입력해야 합니다.",
            "tool": "compare_one_vs_all"
        }

    ref_col = str(reference_period).strip()
    if ref_col not in df.columns:
        return {"error": f"데이터셋에 기준 시점 열 '{ref_col}'이 존재하지 않습니다.", "tool": "compare_one_vs_all"}

    work_df = df.copy()
    l_col = find_label_column(work_df, label_column)
    total_original = len(work_df)
    excluded_reasons = []

    if exclude_aggregates:
        agg_mask = get_aggregate_mask(work_df, l_col)
        agg_cnt = int(agg_mask.sum())
        if agg_cnt > 0:
            work_df = work_df[~agg_mask].copy()
            excluded_reasons.append(f"지역·소득 집계 행 {agg_cnt}개 제외")

    work_df["_numeric_val"] = pd.to_numeric(work_df[ref_col], errors="coerce")
    valid_df = work_df.dropna(subset=["_numeric_val"]).copy()
    null_cnt = len(work_df) - len(valid_df)
    if null_cnt > 0:
        excluded_reasons.append(f"{ref_col}년 결측 행 {null_cnt}개 제외")

    used_rows = len(valid_df)
    total_excluded = total_original - used_rows
    excluded_reason = ", ".join(excluded_reasons) if excluded_reasons else "없음"

    if used_rows == 0:
        return {"error": f"'{ref_col}' 컬럼에 유효한 데이터가 없습니다.", "tool": "compare_one_vs_all"}

    # 타겟 항목 찾기 (지정된 항목이 없으면 "Korea" 포함 항목 또는 1위 항목 선택)
    target_row = valid_df[valid_df[l_col] == target_item]
    if target_row.empty:
        korea_matches = valid_df[valid_df[l_col].astype(str).str.contains("Korea", case=False, na=False)]
        if not korea_matches.empty:
            target_row = korea_matches.iloc[[0]]
            target_item = str(target_row[l_col].values[0])
        else:
            target_row = valid_df.iloc[[0]]
            target_item = str(target_row[l_col].values[0])

    target_val = round(float(target_row["_numeric_val"].values[0]), 2)

    # 전체 통계치 계산
    overall_mean = round(float(valid_df["_numeric_val"].mean()), 2)
    overall_median = round(float(valid_df["_numeric_val"].median()), 2)
    overall_min = round(float(valid_df["_numeric_val"].min()), 2)
    overall_max = round(float(valid_df["_numeric_val"].max()), 2)

    # 타겟 항목의 전체 순위 및 백분위
    rank = int((valid_df["_numeric_val"] > target_val).sum()) + 1
    percentile = round((1 - (rank - 1) / used_rows) * 100, 1)

    chart_labels = [f"선택: {target_item}", "전체 평균", "전체 중앙값", "최고치 (1위)", "최저치 (최하위)"]
    chart_values = [target_val, overall_mean, overall_median, overall_max, overall_min]
    chart_colors = ["#ef4444", "#3b82f6", "#10b981", "#8b5cf6", "#94a3b8"]

    result = {
        "tool": "compare_one_vs_all",
        "reference_period": ref_col,
        "target_item": target_item,
        "used_rows_count": used_rows,
        "excluded_rows_count": total_excluded,
        "excluded_reason": excluded_reason,
        "used_columns": [l_col, ref_col],
        "unit": "%",
        "caution": f"전체 평균은 극단값의 영향을 받으므로 {target_item}의 성과는 중앙값({overall_median}%)과 함께 비교하는 것이 더 공정합니다.",
        "target_value": target_val,
        "rank": rank,
        "total_ranked_items": used_rows,
        "percentile": percentile,
        "overall_mean": overall_mean,
        "overall_median": overall_median,
        "overall_min": overall_min,
        "overall_max": overall_max,
        "chart_data": {
            "type": "bar",
            "labels": chart_labels,
            "datasets": [
                {
                    "label": f"{target_item} vs 전체 비교 수치",
                    "data": chart_values,
                    "backgroundColor": chart_colors
                }
            ]
        }
    }
    return clean_nan(result)


# ==============================================================================
# 도구 카탈로그 정의 (Step 17 Gemini 제안 및 Step 18 도구 디스패치용)
# ==============================================================================
TOOL_CATALOG = [
    {
        "name": "reshape_wide_to_long",
        "label": "와이드 → 롱 포맷 변환",
        "description": "연도 열을 행으로 접어 시계열 분석에 최적화된 표로 재구성합니다.",
        "requires_reference_period": False
    },
    {
        "name": "quality_report",
        "label": "데이터 종합 품질 보고서",
        "description": "열별 결측치율, 중복, 데이터 타입, Unnamed 열을 종합 검사합니다.",
        "requires_reference_period": False
    },
    {
        "name": "year_coverage",
        "label": "연도별 데이터 충실도 분석",
        "description": "연도별 값 입력 개수를 분석하고 가장 안정적인 권장 기준연도를 찾습니다.",
        "requires_reference_period": False
    },
    {
        "name": "detect_aggregates",
        "label": "집계 행(World, 소득그룹) 탐지",
        "description": "전 세계, OECD 등 개별 항목과 중복 계산될 수 있는 묶음 행을 찾아냅니다.",
        "requires_reference_period": False
    },
    {
        "name": "describe_numeric",
        "label": "기술 통계량 분석",
        "description": "평균, 중앙값, 표준편차, 사분위수 등 수치의 기초 통계량을 계산합니다.",
        "requires_reference_period": False
    },
    {
        "name": "trend_line",
        "label": "시계열 추이 선 차트",
        "description": "선택한 항목들(한국, 미국 등)의 연도별 시계열 변화를 꺾은선 그래프로 비교합니다.",
        "requires_reference_period": False
    },
    {
        "name": "top_bottom_n",
        "label": "상위·하위 N 순위 분석",
        "description": "기준연도 기준 상위 N개 및 하위 N개 항목의 순위와 수평 막대 차트를 제공합니다.",
        "requires_reference_period": True
    },
    {
        "name": "group_summary",
        "label": "그룹·구간별 요약 집계",
        "description": "그룹 또는 수치 구간별 평균, 합계, 개수를 집계하여 막대 차트로 비교합니다.",
        "requires_reference_period": True
    },
    {
        "name": "change_rate",
        "label": "두 시점 간 변화량·변화율 분석",
        "description": "과거 시점과 기준 시점 사이의 증가폭(%p)과 성장률(%)을 비교합니다.",
        "requires_reference_period": True
    },
    {
        "name": "distribution",
        "label": "수치 구간별 분포 (히스토그램)",
        "description": "기준연도 수치의 구간별 빈도수를 히스토그램 막대 그래프로 시각화합니다.",
        "requires_reference_period": True
    },
    {
        "name": "correlation_scatter",
        "label": "상관관계 산점도 분석",
        "description": "두 변수 간 피어슨 상관계수(r)와 산점도를 시각화합니다. (상관!=인과)",
        "requires_reference_period": True
    },
    {
        "name": "compare_one_vs_all",
        "label": "단일 항목 vs 전체 평균 강조 비교",
        "description": "특정 국가(예: 한국)의 성과를 전체 평균, 중앙값, 1위와 대조하여 강조합니다.",
        "requires_reference_period": True
    }
]

TOOL_FUNCTIONS = {
    "reshape_wide_to_long": reshape_wide_to_long,
    "quality_report": quality_report,
    "year_coverage": year_coverage,
    "detect_aggregates": detect_aggregates,
    "describe_numeric": describe_numeric,
    "trend_line": trend_line,
    "top_bottom_n": top_bottom_n,
    "group_summary": group_summary,
    "change_rate": change_rate,
    "distribution": distribution,
    "correlation_scatter": correlation_scatter,
    "compare_one_vs_all": compare_one_vs_all
}
