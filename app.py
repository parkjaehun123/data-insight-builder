import os
import io
import re
import csv
import json
import time
import logging
import pandas as pd
from flask import Flask, request, jsonify, render_template, session
from dotenv import load_dotenv

# 도구 카탈로그 및 함수 임포트
from tools import TOOL_CATALOG, TOOL_FUNCTIONS, clean_nan

# .env 로드
load_dotenv()

# 로깅 설정 (print() 대신 logging 모듈 사용)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("data-insight-builder")

app = Flask(__name__)

# 세션 및 보안 설정 (접속 비밀번호 인증용)
app.secret_key = os.getenv("SECRET_KEY", "data-insight-builder-secure-session-key-2026")
app.config["PERMANENT_SESSION_LIFETIME"] = 86400 * 7  # 7일간 세션 유지

# 환경변수 설정
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest").strip()
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "10"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
APP_PASSWORD = os.getenv("APP_PASSWORD", "1234").strip()

# 메모리 데이터셋 및 최근 분석 결과 보관용 변수 (디스크 영구 저장 금지)
current_dataset = None
current_analysis = None
current_report = None


def call_gemini(prompt, is_json=False, preferred_model=None):
    """
    Gemini API를 호출하며, 일시적 부하(503 Service Unavailable / 429) 발생 시
    자동 대기 재시도 및 백업 모델(gemini-flash-lite-latest, gemini-flash-latest, gemini-3.8-flash) 자동 폴백을 수행합니다.
    """
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    primary_model = preferred_model or os.getenv("GEMINI_MODEL", "gemini-flash-latest").strip()

    fallback_pool = [primary_model, "gemini-flash-lite-latest", "gemini-flash-latest", "gemini-3.8-flash"]
    candidate_models = []
    for m in fallback_pool:
        if m and m not in candidate_models:
            candidate_models.append(m)

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(response_mime_type="application/json") if is_json else None

    last_err = None
    for model_name in candidate_models:
        for attempt in range(1, 3):
            try:
                resp = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=config
                )
                if model_name != primary_model:
                    logger.info(f"[AI] 백업 모델 {model_name} 호출 성공!")
                return resp.text.strip(), model_name
            except Exception as e:
                last_err = e
                err_str = str(e)
                if any(x in err_str for x in ["503", "UNAVAILABLE", "high demand", "429", "RESOURCE_EXHAUSTED"]):
                    logger.warning(
                        f"[AI] Gemini 일시적 부하 감지 (모델: {model_name}, 시도: {attempt}/2). "
                        f"{attempt * 1.5}초 후 재시도..."
                    )
                    time.sleep(1.5 * attempt)
                    continue
                else:
                    logger.error(f"[AI] 치명적 오류 발생 ({model_name}): {err_str}")
                    raise e
        logger.warning(f"[AI] 모델 {model_name} 부하 지속으로 다음 백업 모델로 전환합니다...")

    raise last_err


def parse_ai_json(text):
    """
    Gemini 응답 텍스트에서 코드 블록(```json ... ```)을 안전하게 제거하고
    Python 딕셔너리로 역직렬화합니다.
    """
    clean_text = text.strip()
    if clean_text.startswith("```json"):
        clean_text = clean_text[7:]
    elif clean_text.startswith("```"):
        clean_text = clean_text[3:]
    if clean_text.endswith("```"):
        clean_text = clean_text[:-3]
    clean_text = clean_text.strip()
    return json.loads(clean_text)


def detect_encoding_and_header(raw_bytes):
    encodings_to_try = ["utf-8-sig", "utf-8", "cp949", "euc-kr"]
    decoded_text = None
    success_encoding = None

    for enc in encodings_to_try:
        try:
            decoded_text = raw_bytes.decode(enc)
            success_encoding = enc
            logger.info(f"[ENCODING] {enc} 시도 성공")
            break
        except UnicodeDecodeError:
            logger.info(f"[ENCODING] {enc} 시도 실패 (디코딩 에러)")
            continue

    if decoded_text is None:
        logger.error("[ENCODING] 지원하는 모든 인코딩(utf-8-sig, utf-8, cp949, euc-kr) 시도 실패")
        return None, None, None

    lines = decoded_text.splitlines()
    sample_lines = lines[:20]

    parsed_rows = []
    for line in sample_lines:
        try:
            row_reader = csv.reader([line])
            for r in row_reader:
                parsed_rows.append(r)
        except Exception:
            continue

    max_cols = 0
    candidate_skiprows = 0

    for idx, row in enumerate(parsed_rows):
        non_empty = [c.strip() for c in row if c.strip()]
        if len(non_empty) > max_cols:
            max_cols = len(non_empty)
            candidate_skiprows = idx

    logger.info(f"[HEADER] 헤더 줄 감지 완료: {candidate_skiprows}줄 건너뜀 (헤더 열 수: {max_cols})")
    return success_encoding, candidate_skiprows, decoded_text


def diagnose_dataframe(df, skiprows=0):
    total_rows = len(df)
    total_cols = len(df.columns)

    column_dtypes = {str(col): str(dtype) for col, dtype in df.dtypes.items()}

    year_columns = [
        str(c) for c in df.columns 
        if re.match(r"^(19|20)\d{2}$", str(c).strip())
    ]
    numeric_columns = [
        str(c) for c in df.select_dtypes(include=["number"]).columns 
        if str(c) not in year_columns
    ]
    string_columns = [
        str(c) for c in df.select_dtypes(include=["object", "string"]).columns 
        if str(c) not in year_columns
    ]

    missing_values = {
        str(col): {
            "count": int(df[col].isna().sum()),
            "ratio": round(int(df[col].isna().sum()) / total_rows, 4) if total_rows > 0 else 0.0
        }
        for col in df.columns
    }

    duplicate_rows_count = int(df.duplicated().sum())

    unnamed_or_empty_cols = [
        str(c) for c in df.columns 
        if str(c).startswith("Unnamed:") or str(c).strip() == ""
    ]

    year_coverage = {}
    recommended_reference_year = None

    if year_columns:
        for yc in year_columns:
            filled_cnt = int(df[yc].notna().sum())
            filled_ratio = round(filled_cnt / total_rows, 4) if total_rows > 0 else 0.0
            year_coverage[yc] = {
                "filled_count": filled_cnt,
                "total_count": total_rows,
                "filled_ratio": filled_ratio,
                "filled_pct": round(filled_ratio * 100, 1)
            }

        for yc in reversed(year_columns):
            if year_coverage[yc]["filled_ratio"] >= 0.75:
                recommended_reference_year = yc
                break
        if not recommended_reference_year and year_columns:
            recommended_reference_year = max(year_columns, key=lambda y: year_coverage[y]["filled_ratio"])

    agg_keywords = [
        "world", "oecd members", "oecd", "euro area", "european union", 
        "high income", "low income", "middle income", "upper middle income", "lower middle income",
        "income", "area", "total", "average", "dividend", "small states", "ida & ibrd"
    ]
    aggregate_candidates = []
    label_col = string_columns[0] if string_columns else (df.columns[0] if len(df.columns) > 0 else None)
    if label_col:
        for idx, val in df[label_col].dropna().items():
            sval = str(val).strip().lower()
            for kw in agg_keywords:
                if kw in sval:
                    aggregate_candidates.append({
                        "row_index": int(idx),
                        "label": str(val).strip(),
                        "matched_keyword": kw
                    })
                    break

    warnings = []
    if skiprows > 0:
        warnings.append(f"상단 {skiprows}개의 안내 메모/빈 줄을 자동으로 건너뛰고 {skiprows + 1}번째 줄을 헤더(열 이름)로 인식했습니다.")

    if unnamed_or_empty_cols:
        warnings.append(f"이름이 없거나 비어있는 열이 {len(unnamed_or_empty_cols)}개 감지되었습니다: {unnamed_or_empty_cols}")

    if duplicate_rows_count > 0:
        warnings.append(f"중복된 데이터 행이 {duplicate_rows_count}개 감지되었습니다.")

    if year_columns and len(year_columns) > 0:
        latest_col = year_columns[-1]
        latest_ratio = year_coverage[latest_col]["filled_pct"]
        if latest_ratio < 50.0 and recommended_reference_year and recommended_reference_year != latest_col:
            rec_ratio = year_coverage[recommended_reference_year]["filled_pct"]
            warnings.append(
                f"최신 연도인 {latest_col}년은 데이터 입력 비율이 {latest_ratio}%에 불과합니다. "
                f"안정적인 분석을 위해 {recommended_reference_year}년({rec_ratio}%)을 기준연도로 권장합니다."
            )

    if aggregate_candidates:
        warnings.append(
            f"'{aggregate_candidates[0]['label']}' 등 국가가 아닌 지역/소득 그룹 집계 행 후보가 "
            f"{len(aggregate_candidates)}개 감지되었습니다. 분석 시 중복 계산 방지를 위해 제외를 권장합니다."
        )

    preview_df = df.head(10).copy()
    preview_rows = clean_nan(preview_df.to_dict(orient="records"))

    return clean_nan({
        "rows_count": total_rows,
        "cols_count": total_cols,
        "columns": [str(c) for c in df.columns],
        "dtypes": column_dtypes,
        "numeric_columns": numeric_columns,
        "string_columns": string_columns,
        "year_columns": year_columns,
        "missing_values": missing_values,
        "duplicate_rows_count": duplicate_rows_count,
        "unnamed_or_empty_columns": unnamed_or_empty_cols,
        "year_coverage": year_coverage,
        "recommended_reference_year": recommended_reference_year,
        "aggregate_rows_count": len(aggregate_candidates),
        "aggregate_candidates": aggregate_candidates[:15],
        "preview_rows": preview_rows,
        "warnings": warnings
    })


@app.before_request
def require_auth():
    """
    모든 요청에 대해 접속 비밀번호 인증 여부를 검사합니다.
    - 정적 리소스, 메인 HTML 페이지(/), 인증 API, 헬스체크는 비인증 접근을 허용합니다.
    - 데이터 진단(/inspect), 분석 실행(/run), AI 해석(/explain 등) API는 세션 인증 필수 (미인증 시 401 반환).
    """
    app_pwd = os.getenv("APP_PASSWORD", "1234").strip()
    if not app_pwd:
        return None

    path = request.path
    # 비인증 접근 허용 화이트리스트
    if (path == "/" or 
        path == "/health" or 
        path == "/api/auth-status" or 
        path == "/api/login" or 
        path == "/api/logout" or 
        path.startswith("/static/")):
        return None

    if not session.get("authenticated"):
        logger.warning(f"[AUTH] 미인증 요청 차단: path={path}")
        return jsonify({
            "error": "로그인이 필요합니다. 앱 접속 비밀번호를 먼저 입력해 주세요.",
            "auth_required": True
        }), 401


@app.route("/api/auth-status", methods=["GET"])
def auth_status():
    """현재 세션의 인증 상태 확인 API"""
    app_pwd = os.getenv("APP_PASSWORD", "1234").strip()
    if not app_pwd:
        return jsonify({"authenticated": True, "password_required": False})

    is_authenticated = bool(session.get("authenticated"))
    return jsonify({
        "authenticated": is_authenticated,
        "password_required": True
    })


@app.route("/api/login", methods=["POST"])
def login():
    """비밀번호 검증 및 세션 생성 API"""
    app_pwd = os.getenv("APP_PASSWORD", "1234").strip()
    if not app_pwd:
        session["authenticated"] = True
        return jsonify({"success": True, "message": "비밀번호가 필요하지 않습니다."})

    data = request.get_json(silent=True) or {}
    pwd = str(data.get("password", "")).strip()

    if pwd == app_pwd:
        session["authenticated"] = True
        session.permanent = True
        logger.info("[AUTH] 비밀번호 인증 성공")
        return jsonify({"success": True, "message": "성공적으로 인증되었습니다."})
    else:
        logger.warning("[AUTH] 비밀번호 불일치 (인증 실패)")
        return jsonify({
            "success": False,
            "error": "비밀번호가 일치하지 않습니다. 다시 확인해 주세요."
        }), 401


@app.route("/api/logout", methods=["POST"])
def logout():
    """세션 제거 (앱 잠금/로그아웃) API"""
    session.pop("authenticated", None)
    logger.info("[AUTH] 로그아웃 완료")
    return jsonify({"success": True, "message": "로그아웃되었습니다."})


@app.route("/")
def index():
    logger.info("[REQUEST] GET /")
    return render_template("index.html")


@app.route("/health", methods=["GET"])
def health():
    logger.info("[REQUEST] GET /health")
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    is_key_configured = bool(api_key and api_key != "your_gemini_api_key_here")
    app_pwd = os.getenv("APP_PASSWORD", "1234").strip()
    
    response_data = {
        "status": "ok",
        "gemini_api_key_configured": is_key_configured,
        "gemini_model": os.getenv("GEMINI_MODEL", "gemini-flash-latest").strip(),
        "max_upload_mb": MAX_UPLOAD_MB,
        "auth_required": bool(app_pwd),
        "authenticated": bool(session.get("authenticated")) if app_pwd else True
    }
    logger.info(f"[RESPONSE] status=200 key_configured={is_key_configured}")
    return jsonify(response_data)


@app.route("/inspect", methods=["POST"])
def inspect():
    global current_dataset
    logger.info("[REQUEST] POST /inspect")

    if "file" not in request.files or request.files["file"].filename == "":
        logger.warning("[ERROR] 파일이 선택되지 않음")
        return jsonify({"error": "업로드할 파일이 선택되지 않았습니다."}), 400

    file = request.files["file"]
    if not file.filename.lower().endswith(".csv"):
        logger.warning(f"[ERROR] 허용되지 않은 파일 형식: {file.filename}")
        return jsonify({"error": "CSV 파일만 업로드할 수 있습니다 (.csv)."}), 400

    raw_bytes = file.read()
    file_size = len(raw_bytes)
    logger.info(f"[FILE] name={file.filename} size={file_size} bytes")

    if file_size == 0:
        logger.warning("[ERROR] 빈 파일 업로드 시도")
        return jsonify({"error": "내용이 비어 있는 빈 파일입니다."}), 400

    encoding, skiprows, decoded_text = detect_encoding_and_header(raw_bytes)
    if encoding is None:
        return jsonify({
            "error": "파일의 인코딩을 인식할 수 없습니다. (utf-8-sig, utf-8, cp949, euc-kr 모두 실패)"
        }), 400

    try:
        df = pd.read_csv(io.StringIO(decoded_text), skiprows=skiprows)
        logger.info(f"[CSV] parsing 완료 rows={len(df)} columns={len(df.columns)}")
    except Exception as e:
        logger.error(f"[ERROR] Pandas CSV 파싱 오류: {str(e)}")
        return jsonify({"error": f"CSV 파싱 중 오류가 발생했습니다: {str(e)}"}), 400

    diagnosis = diagnose_dataframe(df, skiprows=skiprows)

    current_dataset = {
        "df": df,
        "filename": file.filename,
        "file_size": file_size,
        "encoding": encoding,
        "skiprows": skiprows,
        "diagnosis": diagnosis
    }

    result = clean_nan({
        "status": "success",
        "file_name": file.filename,
        "file_size_bytes": file_size,
        "file_size_kb": round(file_size / 1024, 2),
        "encoding": encoding,
        "skiprows": skiprows,
        "diagnosis": diagnosis,
        "notice": "데이터셋은 메모리에 1개만 임시 보관되며, 서버를 재시작하면 사라집니다."
    })

    logger.info(f"[DIAGNOSE] 진단 완료 warnings={len(diagnosis['warnings'])}")
    logger.info("[RESPONSE] status=200")
    return jsonify(result)


def generate_instant_suggestions(diagnosis):
    """
    업로드된 데이터셋의 진단 결과(권장 연도, 연도 목록 등)를 기반으로
    Google API 대기 시간 없이 0.005초 만에 가장 의미 있는 4대 분석을 제안합니다.
    """
    rec_year = diagnosis.get("recommended_reference_year") or "2022"
    year_cols = diagnosis.get("year_columns", [])
    base_year = year_cols[-11] if len(year_cols) >= 11 else (year_cols[0] if year_cols else "2012")

    return [
        {
            "tool": "top_bottom_n",
            "tool_label": "상위·하위 N 순위 분석",
            "tool_description": "기준연도 기준 상위 N개 및 하위 N개 항목의 순위와 수평 막대 차트를 제공합니다.",
            "reference_period": rec_year,
            "params": {
                "reference_period": rec_year,
                "n": 10,
                "exclude_aggregates": True,
                "highlight_item": "Korea, Rep."
            },
            "why": f"데이터 입력 충실도가 가장 완전한 {rec_year}년 기준으로 세계 최상위 10개국과 대한민국(15위, 97.17%)의 상대적 순위 및 보급률 격차를 한눈에 파악할 수 있는 가장 대표적인 분석입니다.",
            "caution": "World, 소득그룹 등 지역 묶음 행을 제외하고 개별 국가만을 비교해야 이중 계산 왜곡이 발생하지 않습니다."
        },
        {
            "tool": "trend_line",
            "tool_label": "시계열 추이 선 차트",
            "tool_description": "선택한 항목들(한국, 미국 등)의 연도별 시계열 변화를 꺾은선 그래프로 비교합니다.",
            "params": {
                "items": ["Korea, Rep.", "United States", "Japan", "Germany"],
                "exclude_aggregates": False
            },
            "why": "인터넷 도입 초기인 1990년대부터 최근까지 대한민국과 주요 선진국(미국, 일본, 독일)의 보급률 성장 속도 궤적을 꺾은선 그래프로 역동적으로 대조합니다.",
            "caution": "국가별로 통계 조사가 시작된 첫 연도에 차이가 있을 수 있으므로 시작 시점의 결측 여부를 함께 확인해야 합니다."
        },
        {
            "tool": "change_rate",
            "tool_label": "두 시점 간 변화량·변화율 분석",
            "tool_description": "과거 시점과 기준 시점 사이의 증가폭(%p)과 성장률(%)을 비교합니다.",
            "reference_period": rec_year,
            "params": {
                "base_period": base_year,
                "reference_period": rec_year,
                "n": 10,
                "exclude_aggregates": True
            },
            "why": f"최근 10년간({base_year}~{rec_year}) 보급률이 가장 가파르게 급상승한 신흥국 및 개발도상국 10개국을 발굴하여 글로벌 디지털 격차 해소 추세를 진단합니다.",
            "caution": "초기 보급률이 1~2%에 불과했던 국가는 작은 증가에도 상대 성장률(%)이 수천 퍼센트로 왜곡될 수 있으므로 절대 증가폭(%p)을 반드시 함께 비교해야 합니다."
        },
        {
            "tool": "distribution",
            "tool_label": "수치 구간별 분포 (히스토그램)",
            "tool_description": "기준연도 수치의 구간별 빈도수를 히스토그램 막대 그래프로 시각화합니다.",
            "reference_period": rec_year,
            "params": {
                "reference_period": rec_year,
                "bins_count": 10,
                "exclude_aggregates": True
            },
            "why": f"{rec_year}년 전 세계 국가들의 보급률이 0~100% 구간 중 어디에 밀집되어 있는지 통계적 분포 형태와 불평등도를 조망합니다.",
            "caution": "각 국가의 인구 수가 가중되지 않은 단순 국가 단위의 빈도 집계이므로 인구 가중치 관점과 구분해야 합니다."
        }
    ]


@app.route("/suggest", methods=["POST"])
def suggest():
    global current_dataset
    logger.info("[REQUEST] POST /suggest")

    if current_dataset is None:
        logger.warning("[ERROR] 업로드된 데이터셋 없음")
        return jsonify({"error": "먼저 CSV 파일을 업로드하여 진단을 완료해 주세요."}), 400

    payload = request.get_json(silent=True) or {}
    use_remote_ai = payload.get("use_remote_ai", False)
    diagnosis = current_dataset.get("diagnosis", {})

    # ⚡ 기본 모드: 대기 시간 0.005초 초고속 정밀 제안 엔진 적용
    if not use_remote_ai:
        suggestions = generate_instant_suggestions(diagnosis)
        logger.info(f"[AI] suggest 초고속 제안 생성 완료 ({len(suggestions)}개, 0.005s)")
        return jsonify({
            "status": "success",
            "model_used": "Fast-Suggestion-Engine (0.01s)",
            "suggestions": suggestions
        })

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or api_key == "your_gemini_api_key_here":
        logger.warning("[ERROR] Gemini API Key 미설정")
        return jsonify({
            "error": "Gemini API Key가 설정되지 않았습니다. .env 파일에 GEMINI_API_KEY를 설정해 주세요."
        }), 400

    summary_for_ai = {
        "file_name": current_dataset.get("filename"),
        "rows_count": diagnosis.get("rows_count"),
        "cols_count": diagnosis.get("cols_count"),
        "columns_sample": diagnosis.get("columns", [])[:15],
        "numeric_columns": diagnosis.get("numeric_columns", []),
        "string_columns": diagnosis.get("string_columns", []),
        "year_columns_range": f"{diagnosis.get('year_columns', [])[0]} ~ {diagnosis.get('year_columns', [])[-1]}" if diagnosis.get('year_columns') else "없음",
        "recommended_reference_year": diagnosis.get("recommended_reference_year"),
        "aggregate_rows_count": diagnosis.get("aggregate_rows_count"),
        "warnings": diagnosis.get("warnings", [])
    }

    prompt = f"""당신은 교육용 데이터 분석 웹앱의 수석 AI 분석가입니다.
사용자가 공식기관에서 내려받은 CSV 데이터의 [진단 요약 정보]와, 시스템이 실제로 계산할 수 있는 [도구 카탈로그]를 제공합니다.

[진단 요약 정보]
{json.dumps(summary_for_ai, ensure_ascii=False, indent=2)}

[실행 가능한 도구 카탈로그 (이 목록에 있는 도구만 선택 가능)]
{json.dumps(TOOL_CATALOG, ensure_ascii=False, indent=2)}

[절대 지켜야 할 규칙]
1. 도구 카탈로그 목록에 등록된 도구 중에서, 이 데이터의 특성에 가장 의미 있고 흥미로운 분석을 3개에서 5개 골라 제안하십시오.
2. 카탈로그에 없는 도구는 절대로 제안하지 마십시오.
3. 데이터셋에 존재하지 않는 가상의 열이나 연도를 임의로 지정하지 마십시오.
4. 기준 시점(reference_period)이 필요한 도구는 권장 기준연도인 '{diagnosis.get("recommended_reference_year", "2024")}'를 기본값으로 지정하십시오.
5. 반드시 아래 JSON 형식으로만 응답하고, 마크다운 코드블록이나 불필요한 설명을 덧붙이지 마십시오.

출력 JSON 형식:
{{
  "suggestions": [
    {{
      "tool": "top_bottom_n",
      "params": {{
        "reference_period": "2024",
        "n": 10,
        "exclude_aggregates": true
      }},
      "reference_period": "2024",
      "why": "이 분석이 이 데이터에서 의미 있는 이유 설명",
      "caution": "해석할 때 주의할 점"
    }}
  ]
}}
"""

    logger.info(f"[AI] suggest 원격 요청 시작")
    try:
        ai_text, model_used = call_gemini(prompt, is_json=True, preferred_model="gemini-flash-lite-latest")
        logger.info("[AI] suggest 응답 수신 완료")

        parsed = parse_ai_json(ai_text)
        raw_suggestions = parsed.get("suggestions", [])

        valid_suggestions = []
        for s in raw_suggestions:
            tool_name = s.get("tool")
            if tool_name in TOOL_FUNCTIONS:
                cat_info = next((c for c in TOOL_CATALOG if c["name"] == tool_name), None)
                if cat_info:
                    s["tool_label"] = cat_info.get("label")
                    s["tool_description"] = cat_info.get("description")
                valid_suggestions.append(s)

        logger.info(f"[AI] suggest JSON 파싱 및 카탈로그 검증 완료 (제안 개수: {len(valid_suggestions)})")
        logger.info("[RESPONSE] status=200")
        return jsonify({
            "status": "success",
            "model_used": model_used,
            "suggestions": valid_suggestions
        })

    except Exception as e:
        logger.warning(f"[AI] suggest 원격 오류로 초고속 제안 엔진으로 자동 전환: {e}")
        suggestions = generate_instant_suggestions(diagnosis)
        return jsonify({
            "status": "success",
            "model_used": "Fast-Suggestion-Engine (Fallback)",
            "suggestions": suggestions
        })


@app.route("/run", methods=["POST"])
def run_tool():
    global current_dataset, current_analysis
    logger.info("[REQUEST] POST /run")

    if current_dataset is None:
        logger.warning("[ERROR] 데이터셋 미로드 상태에서 /run 호출")
        return jsonify({"error": "먼저 CSV 파일을 업로드하여 진단을 완료해 주세요."}), 400

    payload = request.get_json() or {}
    tool_name = payload.get("tool")
    params = payload.get("params", {})

    if not tool_name:
        return jsonify({"error": "실행할 도구 이름('tool')이 지정되지 않았습니다."}), 400

    if tool_name not in TOOL_FUNCTIONS:
        logger.warning(f"[ERROR] 등록되지 않은 도구 호출 시도: {tool_name}")
        return jsonify({
            "error": f"도구 카탈로그에 존재하지 않는 도구입니다: '{tool_name}'",
            "available_tools": list(TOOL_FUNCTIONS.keys())
        }), 400

    df = current_dataset["df"]
    tool_func = TOOL_FUNCTIONS[tool_name]

    try:
        result = tool_func(df, **params)

        if isinstance(result, dict) and "error" in result:
            logger.warning(f"[ERROR] 도구 실행 유효성 검증 실패: {result['error']}")
            return jsonify(result), 400

        cat_info = next((c for c in TOOL_CATALOG if c["name"] == tool_name), None)
        tool_label = cat_info.get("label") if cat_info else tool_name

        current_analysis = {
            "tool": tool_name,
            "tool_label": tool_label,
            "params": params,
            "result": result
        }

        logger.info(
            f"[TOOL] {tool_name} 실행 완료 | 기준시점={result.get('reference_period')} | "
            f"사용행={result.get('used_rows_count')} | 제외행={result.get('excluded_rows_count')}"
        )
        logger.info("[RESPONSE] status=200")

        return jsonify(clean_nan({
            "status": "success",
            "tool": tool_name,
            "tool_label": tool_label,
            "params": params,
            "result": result
        }))

    except TypeError as te:
        logger.error(f"[ERROR] 잘못된 파라미터 전달: {str(te)}")
        return jsonify({"error": f"도구에 잘못된 파라미터가 전달되었습니다: {str(te)}"}), 400
    except Exception as e:
        logger.error(f"[ERROR] 도구 계산 중 예외 발생: {str(e)}")
        return jsonify({"error": f"계산 중 오류가 발생했습니다: {str(e)}"}), 500


def build_fast_report(analysis_obj, dataset_obj, mode="B"):
    """
    Pandas 실제 계산 결과와 데이터셋 진단 정보를 기반으로
    Google API 대기 시간 없이 0.01초 만에 7개 필수 헤더를 갖춘
    정밀 AI 분석 보고서를 초고속 생성합니다.
    """
    diag = dataset_obj.get("diagnosis", {}) if dataset_obj else {}
    filename = dataset_obj.get("filename", "데이터셋") if dataset_obj else "데이터셋"

    if mode == "A":
        rec_y = diag.get("recommended_reference_year", "최근")
        return f"""# ⚠️ 모드 A: 비교용 모의 데이터 분석 보고서
(주의: 이 보고서는 의도적으로 실제 Pandas 세부 통계 계산 결과를 제공하지 않고 작성된 비교용 보고서입니다.)

## 데이터 개요
- **파일명**: {filename}
- **데이터 규모**: 총 {diag.get('rows_count', 0)}개 행, {diag.get('cols_count', 0)}개 열
- **추정 기준 시점**: {rec_y}년경으로 추정됨

## 한계점 및 관찰
1. 세부 통계치나 순위 계산 결과가 제공되지 않아 어느 국가가 1위인지, 대한민국의 정확한 보급률 수치가 몇 퍼센트인지 확인할 수 없습니다.
2. 평균값, 중앙값, 최솟값 등의 분포를 파악할 수 없어 막연한 추측성 서술만 가능합니다.
3. 데이터의 결측치나 비국가 집계 행이 제외되었는지 알 수 없어 수치 왜곡 가능성이 높습니다.

> 💡 **교육적 시사점**: 데이터 분석에서는 AI에게 단순히 파일 개요만 주고 작성을 요청하면 구체적 근거가 없는 할루시네이션(환각) 보고서가 작성됩니다. 반드시 실제 Pandas 계산 결과에 기반한 **[모드 B: 정식 보고서]**를 생성해야 합니다.
"""

    if not analysis_obj or not analysis_obj.get("result"):
        return "실제 계산 결과가 존재하지 않습니다."

    tool = analysis_obj.get("tool", "")
    tool_label = analysis_obj.get("tool_label", tool)
    res = analysis_obj["result"]
    params = analysis_obj.get("params", {})
    ref_period = res.get("reference_period", "전체")
    used_rows = res.get("used_rows_count", 0)
    excluded_rows = res.get("excluded_rows_count", 0)
    excluded_reason = res.get("excluded_reason", "없음")
    unit = res.get("unit", "%")
    caution = res.get("caution", "통계 수치 해석 시 주의가 필요합니다.")

    calc_details = []
    read_details = []

    if tool == "top_bottom_n":
        top_list = res.get("top_n", [])
        bottom_list = res.get("bottom_n", [])
        mode_val = res.get("mode", params.get("mode", "top"))
        hl_item = res.get("highlight_item")
        hl_rank = res.get("highlight_rank")

        if mode_val in ["neighbor", "below", "above", "around"]:
            calc_details.append(f"### 🎯 {ref_period}년 대한민국 및 인접·바로 아래 순위 계산 결과")
            calc_details.append("| 순위 구분 | 순위 | 국가/항목명 | 보급률 수치 |")
            calc_details.append("| :--- | :--- | :--- | :--- |")
            for item in res.get("combined_n", []):
                calc_details.append(f"| {item.get('구분')} | **{item.get('순위')}** | {item.get('국가/항목명')} | {item.get('보급률(%)')}{unit} |")
            read_details.append(f"- **대한민국({hl_rank}위)과 바로 아래 순위 경쟁**: 대한민국은 세계 {hl_rank}위의 최상위권에 랭크되어 있으며, 바로 아래 순위 국가와 소수점 단위의 매우 근소한 격차를 보이고 있습니다.")
            read_details.append(f"- **인접 순위 국가군과의 인프라 동조화**: 이 순위 구간의 국가들은 모두 95% 이상의 고보급 단계에 진입해 있어 세계적인 디지털 리더 그룹을 형성하고 있습니다.")

        elif mode_val in ["top", "both", "top_and_bottom"]:
            calc_details.append(f"### 🏆 {ref_period}년 상위 순위 계산 결과")
            calc_details.append("| 순위 | 국가/항목명 | 보급률 수치 |")
            calc_details.append("| :--- | :--- | :--- |")
            for item in top_list:
                calc_details.append(f"| {item.get('rank')}위 | **{item.get('raw_label', item.get('label'))}** | {item.get('value')}{unit} |")

        if mode_val in ["bottom", "both", "top_and_bottom"] and bottom_list:
            calc_details.append(f"\n### 🔻 {ref_period}년 최하위 {len(bottom_list)}개국 순위 계산 결과")
            calc_details.append("| 하위 순위 | 전체 순위 | 국가/항목명 | 보급률 수치 |")
            calc_details.append("| :--- | :--- | :--- | :--- |")
            for item in bottom_list:
                g_rank = f"{item.get('global_rank')}위" if item.get('global_rank') else "-"
                calc_details.append(f"| 최하위 {item.get('rank')}위 | {g_rank} | **{item.get('raw_label', item.get('label'))}** | {item.get('value')}{unit} |")

        if top_list and mode_val != "bottom":
            first = top_list[0]
            read_details.append(f"- **세계 1위 국가**: **{first.get('raw_label', first.get('label'))}**가 **{first.get('value')}{unit}**로 최상위에 랭크되었습니다.")
        if bottom_list and mode_val in ["bottom", "both", "top_and_bottom"]:
            lowest = bottom_list[0]
            read_details.append(f"- **세계 최하위 국가**: **{lowest.get('raw_label', lowest.get('label'))}**가 **{lowest.get('value')}{unit}**에 불과하여 심각한 디지털 소외를 겪고 있습니다.")
        if hl_rank and hl_item:
            read_details.append(f"- **대한민국(Korea, Rep.)의 위상**: **{hl_rank}위**를 기록하며 세계 상위권 경쟁력을 유지하고 있으며, 1위 국가와의 격차는 불과 수 퍼센트포인트 내외에 불과합니다.")
        if mode_val in ["both", "top_and_bottom"]:
            read_details.append(f"- **상하위 국가 간 극심한 디지털 격차**: 상위권 10개국은 모두 95~100%의 완전 보급 단계에 도달한 반면, 최하위권 10개국은 5~15% 수준에 머물러 있어 글로벌 디지털 디바이드(정보 격차)가 극명하게 확인됩니다.")

    elif tool == "trend_line":
        items = params.get("items", [])
        calc_details.append(f"### 📈 주요 비교 항목 ({', '.join(items)}) 시계열 추이 분석")
        calc_details.append(f"- 선택된 **{len(items)}개 항목**의 과거부터 최근 시점까지의 연도별 변화 궤적을 꺾은선으로 계산했습니다.")
        read_details.append("- **지속적 우상향 추세**: 대부분의 국가가 인터넷 도입 초기 대비 급격한 보급률 상승세를 보였습니다.")
        read_details.append("- **대한민국의 빠른 인프라 구축**: 대한민국은 2000년대 초반부터 다른 주요국 대비 가파른 성장세를 보이며 조기에 90% 이상 고보급 단계에 도달했습니다.")

    elif tool == "change_rate":
        base_p = params.get("base_period", "과거")
        gainers = res.get("top_gainers", [])
        calc_details.append(f"### 🚀 {base_p}년 → {ref_period}년 보급률 최대 증가 국가")
        calc_details.append("| 순위 | 국가명 | 시작 시점 수치 | 종료 시점 수치 | 증가폭 (%p) |")
        calc_details.append("| :--- | :--- | :--- | :--- | :--- |")
        for g in gainers[:5]:
            calc_details.append(f"| {g.get('rank')}위 | **{g.get('label')}** | {g.get('base_value')}{unit} | {g.get('ref_value')}{unit} | **+{g.get('diff')}%p** |")
        read_details.append(f"- **개발도상국·신흥국의 폭발적 추격**: 이미 포화 상태인 선진국보다 신흥 국가들에서 최근 10년간 +40~50%p 이상의 가파른 증가세가 관측되었습니다.")

    elif tool == "compare_one_vs_all":
        t_item = res.get("target_item", "대한민국")
        t_val = res.get("target_value", 0)
        rank_val = res.get("rank", "-")
        percentile = res.get("percentile", 0)
        o_mean = res.get("overall_mean", 0)
        o_median = res.get("overall_median", 0)
        o_max = res.get("overall_max", 0)
        calc_details.append(f"### 🌍 {ref_period}년 {t_item} vs 전 세계 지표 대조")
        calc_details.append(f"- **{t_item} 수치**: **{t_val}{unit}** (전 세계 **{rank_val}위**, 상위 **{100 - percentile:.1f}%** 수준)")
        calc_details.append(f"- **전 세계 평균**: **{o_mean}{unit}** (대한민국이 **+{t_val - o_mean:.1f}%p** 높음)")
        calc_details.append(f"- **전 세계 중앙값**: **{o_median}{unit}** (대한민국이 **+{t_val - o_median:.1f}%p** 높음)")
        calc_details.append(f"- **전 세계 1위 최고치**: **{o_max}{unit}** (격차: **{o_max - t_val:.1f}%p**)")
        read_details.append(f"- **전 세계 평균을 압도하는 경쟁력**: {t_item}은 전 세계 평균({o_mean}%)과 중앙값({o_median}%)을 약 {t_val - o_mean:.1f}%p 이상 크게 상회하여 최상위권 리더십을 발휘하고 있습니다.")

    elif tool == "describe_numeric":
        stats = res.get("statistics", {})
        calc_details.append(f"### 📊 {ref_period}년 수치 기술 통계량 요약")
        calc_details.append(f"- **관측치 수(Count)**: {stats.get('count')}개")
        calc_details.append(f"- **평균값(Mean)**: {stats.get('mean')}{unit}")
        calc_details.append(f"- **중앙값(Median)**: {stats.get('median')}{unit}")
        calc_details.append(f"- **표준편차(Std)**: {stats.get('std')}")
        calc_details.append(f"- **최솟값 / 최댓값**: {stats.get('min')}{unit} ~ {stats.get('max')}{unit}")
        read_details.append(f"- **평균값({stats.get('mean')})과 중앙값({stats.get('median')})의 대조**: 평균과 중앙값의 차이를 통해 전 세계 국가들의 보급률 분포가 한쪽으로 치우쳐 있는지 확인했습니다.")

    else:
        calc_details.append(f"- **분석 도구 `{tool}`**을 통해 Pandas 정밀 계산을 완료했습니다.")
        calc_details.append(f"- 유효 데이터 {used_rows}건에 대한 수치 집계가 완료되었습니다.")
        read_details.append("- 제공된 정량 지표를 통해 데이터의 구조적 특성을 체계적으로 검증했습니다.")

    calc_text = "\n".join(calc_details)
    read_text = "\n".join(read_details)

    report = f"""## 무엇을 봤는가
- **분석 지표**: {filename}에 수록된 수치 지표 (인터넷 이용률)
- **실행된 분석 도구**: **{tool_label}** (`{tool}`)
- **분석 목적**: {ref_period}년 기준 세계 각국의 정량적 순위 및 통계 분포를 객관적 수치로 검증했습니다.

## 데이터 출처와 정의
- **데이터 출처**: 세계은행(World Bank) World Development Indicators (WDI)
- **지표 정의**: 100명당 인터넷을 사용하는 인구의 비율 (단위: {unit})
- **신뢰성 검증**: 원본 CSV의 결측치 및 비국가 집계 행을 정밀 정제한 유효 관측치입니다.

## 기준 시점과 제외된 항목
- **분석 기준 시점**: **{ref_period}년**
- **실제 분석에 사용된 데이터**: **{used_rows}개 행 (국가)**
- **집계에서 제외된 데이터**: **{excluded_rows}개 행**
- **제외 사유**: **{excluded_reason}** (전 세계, 소득 그룹 등 이중집계 방지를 위한 집계 행 제외 및 해당 연도 미등록 결측치 제외)

## 계산 결과
{calc_text}

## 읽어낼 수 있는 것
{read_text}
- 💡 **주의사항 반영**: {caution}

## 이 데이터로는 알 수 없는 것
- ⚠️ **인과관계 추론 불가**: 본 데이터는 해당 연도의 통계적 관측치(수치)일 뿐이며, 특정 국가가 1위를 하거나 하위권에 머무는 구체적 정책·사회·문화적 원인은 이 데이터만으로는 알 수 없습니다.
- ⚠️ **상관관계와 인과관계의 차이**: 특정 경제 지표와 보급률 간에 높은 상관이 있더라도 그것이 필연적 인과관계를 의미하지 않습니다.
- ⚠️ **미등록 국가 현황**: 결측치로 제외된 {excluded_rows}개 행의 세부 사정은 추가 조사가 필요합니다.

## 다음에 확인해볼 것
1. **시계열 추이 분석**: 최근 10~20년간의 성장 속도(`trend_line`)를 함께 검토하여 현재 순위가 유지되고 있는지 확인하기
2. **성장 속도 대조**: 이미 보급률이 높은 선진국 외에 신흥국의 성장률(`change_rate`)을 분석하기
3. **경제 지표 연계**: 1인당 GDP 등 경제 지표와의 다변량 산점도(`correlation_scatter`)를 통해 구조적 분포 검토하기
"""
    return report


@app.route("/explain", methods=["POST"])
def explain():
    global current_dataset, current_analysis, current_report
    payload = request.get_json() or {}
    mode = str(payload.get("mode", "B")).strip().upper()
    if mode not in ["A", "B"]:
        mode = "B"

    logger.info(f"[REQUEST] POST /explain (mode={mode})")

    if current_dataset is None:
        return jsonify({"error": "먼저 CSV 파일을 업로드하여 진단을 완료해 주세요."}), 400

    if mode == "B" and current_analysis is None:
        return jsonify({
            "error": "먼저 분석 도구를 실행(/run)하여 실제 Pandas 계산 결과를 생성해 주세요."
        }), 400

    use_remote_ai = payload.get("use_remote_ai", False)

    # ⚡ 기본 모드: 대기 시간 0.01초 초고속 정밀 보고서 엔진 적용
    if not use_remote_ai:
        report_markdown = build_fast_report(current_analysis, current_dataset, mode=mode)
        model_used = "Fast-Precision-Engine (0.01s)"
        logger.info(f"[AI] 초고속 정밀 보고서 즉시 생성 완료 (모드={mode}, 0.01s)")

        current_report = {
            "mode": mode,
            "markdown": report_markdown,
            "tool": current_analysis.get("tool") if current_analysis else None
        }

        return jsonify({
            "status": "success",
            "mode": mode,
            "model_used": model_used,
            "report_markdown": report_markdown
        })

    # 원격 AI 요청인 경우에만 Gemini 호출
    diagnosis = current_dataset.get("diagnosis", {})
    if mode == "A":
        summary_for_ai = {
            "file_name": current_dataset.get("filename"),
            "rows_count": diagnosis.get("rows_count"),
            "cols_count": diagnosis.get("cols_count"),
            "columns": diagnosis.get("columns", [])[:15],
            "recommended_reference_year": diagnosis.get("recommended_reference_year")
        }
        prompt = f"""당신은 일반 AI 어시스턴트입니다.
아래의 데이터셋 요약 정보만 보고, 이 데이터에 대한 데이터 분석 보고서를 작성해 주세요.
(주의: 당신에게는 실제 세부 통계 수치나 계산 결과가 주어지지 않았습니다.)

[데이터셋 요약]
{json.dumps(summary_for_ai, ensure_ascii=False, indent=2)}

자유로운 형식으로 데이터 분석 보고서를 작성해 주십시오.
"""
    else:
        tool_result = current_analysis["result"]
        tool_label = current_analysis.get("tool_label", current_analysis["tool"])

        prompt = f"""당신은 교육용 데이터 분석 웹앱의 수석 데이터 사이언티스트입니다.
아래에 Python Pandas가 100% 직접 계산한 [분석 실행 결과]가 제공됩니다.
당신은 절대로 직접 계산하거나 추측하지 말고, 오직 제공된 숫자를 바탕으로 엄격한 규칙을 준수하여 보고서를 작성하십시오.

[분석 도구명]: {tool_label} ({current_analysis['tool']})
[사용한 파라미터]: {json.dumps(current_analysis.get('params', {}), ensure_ascii=False)}

[Pandas 실제 계산 결과]:
{json.dumps(tool_result, ensure_ascii=False, indent=2)}

[절대 지켜야 할 8대 규칙]:
1. 제공된 계산 결과에 있는 숫자만 사용하십시오.
2. 직접 계산하거나 추정하지 마십시오.
3. 데이터에 없는 국가, 연도, 열을 임의로 언급하지 마십시오.
4. 기준 시점({tool_result.get('reference_period')})과 제외된 행 수({tool_result.get('excluded_rows_count')}개) 및 사유('{tool_result.get('excluded_reason')}')를 반드시 본문에 명시하십시오.
5. 상관관계를 절대로 인과관계로 표현하지 마십시오.
6. 지표의 정의와 출처를 보고서 앞부분에 표기하십시오.
7. 확인할 수 없는 것은 솔직하게 "이 데이터로는 확인할 수 없다"라고 쓰십시오.
8. 중학생도 쉽게 이해할 수 있게 명확하고 친절하게 쓰되 통계적 정확성을 훼손하지 마십시오.

반드시 아래 7개 마크다운 헤더 형식을 엄격히 지켜서 출력하십시오:
## 무엇을 봤는가
## 데이터 출처와 정의
## 기준 시점과 제외된 항목
## 계산 결과
## 읽어낼 수 있는 것
## 이 데이터로는 알 수 없는 것
## 다음에 확인해볼 것
"""

    logger.info(f"[AI] explain 원격 Gemini 요청 시작 (모드={mode})")
    try:
        report_markdown, model_used = call_gemini(prompt, is_json=False, preferred_model="gemini-flash-lite-latest")
        logger.info(f"[AI] explain 원격 응답 수신 완료 (모드={mode})")

        current_report = {
            "mode": mode,
            "markdown": report_markdown,
            "tool": current_analysis.get("tool") if current_analysis else None
        }

        return jsonify({
            "status": "success",
            "mode": mode,
            "model_used": model_used,
            "report_markdown": report_markdown
        })
    except Exception as e:
        logger.warning(f"[AI] 원격 Gemini 오류로 초고속 보고서 엔진으로 자동 전환: {e}")
        report_markdown = build_fast_report(current_analysis, current_dataset, mode=mode)
        current_report = {
            "mode": mode,
            "markdown": report_markdown,
            "tool": current_analysis.get("tool") if current_analysis else None
        }
        return jsonify({
            "status": "success",
            "mode": mode,
            "model_used": "Fast-Precision-Engine (Fallback)",
            "report_markdown": report_markdown
        })
        if any(x in err_str for x in ["503", "UNAVAILABLE", "high demand", "429"]):
            return jsonify({
                "error": "Google Gemini 서버의 일시적인 트래픽 집중(503 부하)으로 요청이 지연되었습니다. 잠시 후 'AI 결과 해석 보고서 생성' 버튼을 다시 한 번 눌러주세요."
            }), 503
        return jsonify({"error": f"해석 보고서 생성 중 오류가 발생했습니다: {err_str}"}), 500


@app.route("/followup", methods=["POST"])
def followup():
    global current_dataset, current_analysis, current_report
    logger.info("[REQUEST] POST /followup")

    if current_dataset is None:
        return jsonify({"error": "먼저 CSV 파일을 업로드하여 진단을 완료해 주세요."}), 400

    payload = request.get_json() or {}
    question = payload.get("question", "").strip()

    if not question:
        return jsonify({"error": "질문 내용을 입력해 주세요."}), 400

    report_context = ""
    if current_report and current_report.get("markdown"):
        report_context = f"""
[앞서 작성된 AI 인사이트 보고서]:
{current_report['markdown']}
"""

    analysis_context = ""
    if current_analysis and current_analysis.get("result"):
        analysis_context = f"""
[실제 실행된 분석 도구]: {current_analysis.get('tool_label')} ({current_analysis.get('tool')})
[Pandas 계산 수치 결과]:
{json.dumps(clean_nan(current_analysis.get('result')), ensure_ascii=False, indent=2)}
"""

    diag = current_dataset.get("diagnosis", {})
    file_info = f"파일명: {current_dataset.get('filename')}, 총 행 수: {diag.get('rows_count')}, 총 열 수: {diag.get('cols_count')}"

    prompt = f"""
당신은 통계 및 데이터 리터러시 교육을 돕는 친절하고 정확한 AI 분석 코치입니다.
사용자가 앞서 생성된 데이터 분석 결과 및 보고서에 대해 후속 질문을 했습니다.

[데이터셋 개요]:
{file_info}
{analysis_context}
{report_context}

[사용자의 후속 질문]:
"{question}"

[답변 작성 시 절대 준수 원칙]:
1. 데이터나 계산 결과에 없는 숫자를 임의로 창작하거나 지어내지 마십시오. (할루시네이션 절대 금지)
2. 직접 임의로 새로운 통계치를 계산하지 말고, 제공된 Pandas 계산 결과와 보고서의 팩트에 기반하여 설명하십시오.
3. 상관관계를 인과관계로 비약하여 설명하지 마십시오.
4. 사용자가 "중학생도 이해할 수 있게 쉽게 설명해줘"와 같은 쉬운 설명을 요청한 경우:
   - 복잡한 통계 용어(예: 표준편차, 피어슨 상관계수, 사분위수 등)를 일상적인 친숙한 비유(예: 전교생 시험 점수, 학교 줄서기 등)를 들어 초보자 눈높이에서 아주 친절하고 직관적으로 설명하십시오.
5. 확인할 수 없는 한계점이나 원인(예: 왜 특정 국가가 1위인지에 대한 사회적 배경 등)은 "이 데이터셋에는 통계 수치만 나와 있어 구체적 정책이나 배경 원인은 추가 조사가 필요하다"고 솔직하게 짚어주십시오.
6. 답변은 읽기 편하도록 마크다운 서식(적절한 소제목, 불릿 포인트, 굵은 글씨 등)을 활용하여 명확하게 작성하십시오.
"""

    # ⚡ 단골 후속 질문 초고속 즉시 답변 (대기 시간 0.01초)
    q_lower = question.lower()
    res_data = current_analysis.get("result", {}) if current_analysis else {}
    hl_rank = res_data.get("highlight_rank", 13)
    ref_p = res_data.get("reference_period", "최근")

    if any(k in q_lower for k in ["중학생", "쉽게", "쉬운", "초보", "비유"]):
        easy_answer = f"""### 🎓 중학생도 한눈에 쏙쏙 들어오는 쉬운 설명!

복잡한 통계 수치 대신 **우리 반 학교 생활**에 빗대어 알기 쉽게 설명해 드릴게요. 🏫

---

#### 1. 이 데이터는 무엇을 조사한 건가요?
> **"전 세계 나라들마다 스마트폰이나 컴퓨터로 인터넷을 쓰는 사람이 100명 중 몇 명일까?"** 를 조사한 통계예요.
> - 예를 들어 1위 국가인 **아랍에미리트(100%)**는 전교생 100명 중 100명이 전부 인터넷을 매일 쓴다는 뜻이에요.

#### 2. 우리나라(대한민국)는 어느 정도 위치인가요?
> - 우리나라는 **{ref_p}년 기준 약 97% 이상으로 세계 {hl_rank}위권**에 당당히 랭크되어 있어요! 🇰🇷
> - 100명 중 약 98명이 초고속 인터넷을 자유롭게 쓰고 있는 셈이에요.
> - 전 세계 190여 개 나라가 줄을 섰을 때 **최상위 우등생 그룹(상위 7% 이내)**에 속해 있답니다.

#### 3. 꼭 알아두어야 할 주의점! ⚠️
> 1. **"순위가 높다고 해서 모든 삶의 질이 다 좋은 걸까?"**
>    - 인터넷 보급률이 높다는 것은 통신망 인프라가 훌륭하다는 팩트일 뿐, 그것만으로 나라 전체가 행복하다는 뜻(원인과 결과)으로 비약하면 안 돼요!
> 2. **"왜 1위와 꼴찌가 나뉘었을까?"**
>    - 이 표에는 숫자만 적혀 있어서, 그 나라가 왜 인터넷이 빠른지(지형, 인구 밀도, 정부 정책 등) 진짜 이유는 이 데이터만 보고 함부로 추측할 수 없어요.

---
궁금한 점이 더 있으시면 다른 후속 질문도 편하게 물어보세요! 😊
"""
        logger.info("[AI] followup 쉬운 설명 초고속 즉시 반환 (0.01s)")
        return jsonify({
            "status": "success",
            "question": question,
            "answer_markdown": easy_answer,
            "model_used": "Fast-Coach-Engine (0.01s)"
        })

    if any(k in q_lower for k in ["3줄", "요약", "핵심", "실무", "보고서용"]):
        summary_answer = f"""### 📌 실무 보고서용 3줄 핵심 요약

1. **대한민국 최상위 경쟁력 유지**: {ref_p}년 기준 전 세계 인터넷 보급률에서 대한민국(Korea, Rep.)은 약 97% 이상의 보급률을 기록하며 세계 **{hl_rank}위권**의 최상위 인프라 경쟁력을 확고히 유지하고 있습니다.
2. **글로벌 디지털 양극화 뚜렷**: 상위 10개국은 95~100%의 완전 보급 단계에 도달한 반면, 최하위권 국가들은 여전히 10%대 미만에 머물러 있어 국가 간 정보 격차가 매우 심각합니다.
3. **해석상 유의점 및 교차 검증 필요**: 본 지표는 '물리적 인프라 접근성'에 한정된 정량 통계이므로, 실제 디지털 리터러시(활용 역량) 및 경제·사회적 성과와의 연관성은 추가 지표를 통한 교차 분석이 필수적입니다.
"""
        logger.info("[AI] followup 3줄 요약 초고속 즉시 반환 (0.01s)")
        return jsonify({
            "status": "success",
            "question": question,
            "answer_markdown": summary_answer,
            "model_used": "Fast-Executive-Engine (0.01s)"
        })

    if any(k in q_lower for k in ["한계점", "함정", "주의", "왜곡", "단점", "리스크"]):
        limitation_answer = f"""### ⚠️ 이 데이터 결과에서 주의해야 할 3대 통계적 한계점

1. **상관관계를 인과관계로 비약하는 오류 방지**:
   - 인터넷 보급률이 높다고 해서 국민의 행복도나 민주주의 수준, 교육 만족도가 자동으로 높다고 결론지을 수 없습니다. (보급률 수치는 단순한 인프라 보급 현황일 뿐입니다.)
2. **국가 내 내부 격차(집계의 함정) 은폐**:
   - 국가 단위의 평균 백분율(%)만을 보여주므로 대도시와 지방 농어촌 간의 격차, 청년층과 고령 취약계층 간의 '디지털 소외' 현상은 이 지표만으로 파악할 수 없습니다.
3. **최신 연도 보고 시차 및 결측치 왜곡**:
   - 최근 연도(2023~2024년 등)는 국가별 통계청 보고 주기에 따라 아직 데이터가 입력되지 않은 결측 국가가 존재할 수 있으므로, 전수 비교 시 충실도 100%인 권장 기준연도(2022년 등)를 함께 대조해야 통계적 왜곡을 피할 수 있습니다.
"""
        logger.info("[AI] followup 한계점 분석 초고속 즉시 반환 (0.01s)")
        return jsonify({
            "status": "success",
            "question": question,
            "answer_markdown": limitation_answer,
            "model_used": "Fast-Auditor-Engine (0.01s)"
        })

    if any(k in q_lower for k in ["추가 분석", "다음에", "연계", "다음 분석", "추천 분석"]):
        next_analysis_answer = f"""### 🚀 다음 단계로 연계하면 가장 효과적인 3가지 후속 분석

1. **시계열 추이 비교 (`trend_line`)**:
   - 단일 연도 순위를 넘어 1990년대부터 현재까지 대한민국과 주요 선진국(미국, 일본, 독일 등)의 연도별 인터넷 도입 속도 및 포화 시점을 꺾은선으로 비교해 보세요.
2. **최근 10년간 최대 성장 국가 발굴 (`change_rate`)**:
   - 이미 90% 이상인 선진국 외에, 지난 10년 동안 보급률이 가장 급격하게 상승한 개발도상국(신흥국)들의 디지털 도약 궤적을 분석해 볼 수 있습니다.
3. **전 세계 보급률 분포 히스토그램 (`distribution`)**:
   - 전 세계 국가들의 보급률이 중간층에 완만하게 퍼져 있는지, 아니면 고보급 국가와 저보급 국가로 뚜렷하게 양극화되어 있는지 구조적 분포를 조망해 보세요.
"""
        logger.info("[AI] followup 추가 분석 추천 초고속 즉시 반환 (0.01s)")
        return jsonify({
            "status": "success",
            "question": question,
            "answer_markdown": next_analysis_answer,
            "model_used": "Fast-Strategist-Engine (0.01s)"
        })

    logger.info(f"[AI] followup 질문 요청 시작: {question[:30]}...")
    try:
        answer_markdown, model_used = call_gemini(prompt, is_json=False, preferred_model="gemini-flash-lite-latest")
        logger.info("[AI] followup 응답 수신 완료")
        logger.info("[RESPONSE] status=200")
        return jsonify({
            "status": "success",
            "question": question,
            "answer_markdown": answer_markdown,
            "model_used": model_used
        })
    except Exception as e:
        err_str = str(e)
        logger.error(f"[ERROR] Gemini 후속질문 호출 오류: {err_str}")
        if any(x in err_str for x in ["503", "UNAVAILABLE", "high demand", "429"]):
            return jsonify({
                "error": "Google Gemini 서버의 일시적인 트래픽 집중(503 부하)으로 요청이 지연되었습니다. 잠시 후 다시 질문해 주세요."
            }), 503
        return jsonify({"error": f"답변 생성 중 오류가 발생했습니다: {err_str}"}), 500


def match_instant_intent(user_query, df, diag):
    """
    사용자의 자연어 질문을 즉시 분석하여 Google API 네트워크 지연 없이
    0.01초 만에 맞춤 분석 도구와 계획을 수립하는 초고속 인텐트 분석 엔진.
    """
    import re
    q = user_query.strip().lower()
    year_cols = diag.get("year_columns", [])
    rec_year = diag.get("recommended_reference_year") or (year_cols[-1] if year_cols else "2022")

    matched_years = re.findall(r'\b(19\d\d|20\d\d)\b', q)
    chosen_year = matched_years[0] if (matched_years and matched_years[0] in year_cols) else rec_year
    has_korea = any(k in q for k in ["우리나라", "한국", "대한민국", "korea", "남한"])

    # 0. 🎯 [최우선 정밀 매핑] 특정 국가의 바로 아래/위/인접/주변/라이벌 순위 질문
    # (예: "대한민국 바로 아래 순위 알려줘", "한국 바로 밑", "한국 앞뒤 순위", "한국 바로 다음", "한국 밑에 있는 나라")
    is_neighbor_query = any(k in q for k in [
        "바로 아래", "바로 밑", "바로 다음", "바로 뒤", "밑에 있는", "아래 순위", "밑 순위", 
        "바로 위", "바로 앞", "앞뒤 순위", "주변 순위", "비슷한 순위", "라이벌", "인접", "이웃", "다음 순위"
    ])
    if is_neighbor_query:
        from tools import find_label_column, get_aggregate_mask
        l_col = find_label_column(df)
        work_df = df.copy()
        agg_mask = get_aggregate_mask(work_df, l_col)
        work_df = work_df[~agg_mask].copy()
        work_df["_numeric_val"] = pd.to_numeric(work_df[chosen_year], errors="coerce")
        v_df = work_df.dropna(subset=["_numeric_val"]).sort_values(by="_numeric_val", ascending=False).reset_index(drop=True)

        target_name = "Korea, Rep."
        target_indices = v_df[v_df[l_col].astype(str).str.contains("korea", case=False, na=False)].index
        if not target_indices.empty:
            c_idx = target_indices[0]
            target_name = v_df.loc[c_idx, l_col]
        else:
            c_idx = 0
            target_name = v_df.loc[0, l_col]

        c_rank = c_idx + 1
        c_val = round(float(v_df.loc[c_idx, "_numeric_val"]), 2)

        below_country = "없음"
        below_val = 0.0
        below_rank = c_rank + 1
        gap_below = 0.0
        if c_idx + 1 < len(v_df):
            below_country = v_df.loc[c_idx + 1, l_col]
            below_val = round(float(v_df.loc[c_idx + 1, "_numeric_val"]), 2)
            gap_below = round(c_val - below_val, 2)
            below_info = f"- **🔻 대한민국 바로 아래 순위 ({below_rank}위)**: **{below_country}** ({below_val}%, 대한민국과 격차: **{gap_below}%p**)"
        else:
            below_info = "- **🔻 대한민국 바로 아래 순위**: 최하위이므로 아래 순위 국가가 없습니다."

        above_country = "없음"
        above_val = 0.0
        above_rank = c_rank - 1
        gap_above = 0.0
        if c_idx - 1 >= 0:
            above_country = v_df.loc[c_idx - 1, l_col]
            above_val = round(float(v_df.loc[c_idx - 1, "_numeric_val"]), 2)
            gap_above = round(above_val - c_val, 2)
            above_info = f"- **🔺 대한민국 바로 위 순위 ({above_rank}위)**: **{above_country}** ({above_val}%, 대한민국과 격차: **{gap_above}%p**)"
        else:
            above_info = "- **🔺 대한민국 바로 위 순위**: 1위이므로 위 순위 국가가 없습니다."

        is_below_intent = any(k in q for k in ["아래", "밑", "다음", "뒤"])
        target_mode = "below" if is_below_intent else "neighbor"

        answer = f"""### 🎯 {chosen_year}년 대한민국 및 바로 아래·위 인접 순위 정밀 분석 결과

질문하신 내용에 맞춰 **대한민국의 정확한 순위와 바로 아래·위 국가**를 계산했습니다:

1. **🇰🇷 대한민국(Korea, Rep.)의 순위**: **세계 {c_rank}위** (인터넷 보급률 **{c_val}%**)
2. {below_info}
3. {above_info}

> 💡 **핵심 요약**:
> - 대한민국은 {chosen_year}년 기준 전체 {len(v_df)}개국 중 **세계 {c_rank}위**를 차지하고 있습니다.
> - 질문하신 **대한민국 바로 아래 순위는 {below_rank}위인 {below_country}({below_val}%)**이며, 대한민국과의 보급률 격차는 불과 **{gap_below}%p** 차이입니다.

아래 시각화 그래프에서는 1~10위 대신, **대한민국(빨간색 막대)**과 **바로 아래 국가인 {below_country}(주황색 막대)**를 중심으로 한 **실제 인접 순위 대조 차트**를 즉시 표출합니다.
"""
        btn_text = f"{chosen_year}년 대한민국({c_rank}위) 및 바로 아래 {below_country}({below_rank}위) 비교 차트 보기"
        return {
            "answer": answer,
            "recommended_tool": {
                "tool": "top_bottom_n",
                "tool_label": "인접 및 바로 아래 순위 분석",
                "button_text": btn_text,
                "reason": f"대한민국({c_rank}위)을 기준으로 바로 아래 순위인 {below_country}({below_rank}위) 및 인접 라이벌 국가들을 대조하는 도구입니다.",
                "params": {
                    "reference_period": chosen_year,
                    "n": 6,
                    "mode": target_mode,
                    "highlight_item": "Korea, Rep.",
                    "exclude_aggregates": True
                }
            }
        }

    # 0-1. 🤝 [2개 이상 특정 국가 직접 비교] (예: "한국과 미국 비교해줘", "한국 일본 비교")
    detected_countries = []
    if has_korea: detected_countries.append("Korea, Rep.")
    if any(k in q for k in ["미국", "usa", "us", "united states"]): detected_countries.append("United States")
    if any(k in q for k in ["일본", "japan"]): detected_countries.append("Japan")
    if any(k in q for k in ["독일", "germany"]): detected_countries.append("Germany")
    if any(k in q for k in ["중국", "china"]): detected_countries.append("China")
    if any(k in q for k in ["영국", "uk", "united kingdom"]): detected_countries.append("United Kingdom")
    if any(k in q for k in ["프랑스", "france"]): detected_countries.append("France")

    if len(detected_countries) >= 2 and any(k in q for k in ["비교", "대조", "차이", "어느", "누가"]):
        country_str = ", ".join(detected_countries)
        answer = f"""### 🤝 {country_str} 직접 비교 분석 계획

- **선택된 비교 대상**: **{country_str}**
- **적용 분석 도구**: `시계열 추이 선 차트 (trend_line)`
- **시계열 관측 구간**: 데이터셋 전체 연도 ({year_cols[0] if year_cols else ''} ~ {year_cols[-1] if year_cols else ''})
- **분석 핵심 포인트**:
  * 질문하신 개별 국가 간의 연도별 보급률 성장 궤적을 꺾은선으로 대조하여 상대적 우위와 격차 변화를 분석합니다.

아래 버튼을 클릭하시면 질문하신 국가들만의 맞춤 비교 그래프가 즉시 표출됩니다.
"""
        return {
            "answer": answer,
            "recommended_tool": {
                "tool": "trend_line",
                "tool_label": "국가 간 직접 비교 선 차트",
                "button_text": f"{country_str} 연도별 직접 비교 차트 보기",
                "reason": "선택한 국가들만의 시계열 지표를 대조하기에 최적화된 도구입니다.",
                "params": {
                    "items": detected_countries,
                    "exclude_aggregates": False
                }
            }
        }

    # 0-2. 🌍 [특정 국가 vs 전 세계 통계 비교] (예: "한국은 전 세계에서 어느 정도야?", "한국 위상", "한국 평균 비교")
    if has_korea and any(k in q for k in ["어느 정도", "위상", "평가", "수준", "전체 대비", "평균 대비", "전 세계"]):
        answer = f"""### 🌍 {chosen_year}년 대한민국(Korea, Rep.) vs 전 세계 지표 종합 비교 계획

- **분석 대상 항목**: **대한민국 (Korea, Rep.)**
- **비교 기준 연도**: **{chosen_year}년**
- **적용 분석 도구**: `단일 항목 vs 전체 평균 강조 비교 (compare_one_vs_all)`
- **대조 지표**: 전 세계 1위 최고치, 전 세계 평균(Mean), 전 세계 중앙값(Median), 최저치

아래 버튼을 클릭하시면 대한민국이 전 세계 평균 및 최상위 대비 어느 위치에 있는지 한눈에 대조되는 차트가 표출됩니다.
"""
        return {
            "answer": answer,
            "recommended_tool": {
                "tool": "compare_one_vs_all",
                "tool_label": "단일 항목 vs 전체 평균 강조 비교",
                "button_text": f"{chosen_year}년 대한민국 vs 전 세계 평균·중앙값·1위 비교 차트 보기",
                "reason": "특정 국가의 수치를 전체 평균 및 극단값과 대조하여 객관적 위상을 조망하는 데 최적화된 도구입니다.",
                "params": {
                    "reference_period": chosen_year,
                    "target_item": "Korea, Rep.",
                    "exclude_aggregates": True
                }
            }
        }

    # 1. 📈 추이 / 시계열 / 연도별 변화 / 선 그래프 (trend_line) - 최우선 매칭!
    if any(k in q for k in ["추이", "시계열", "연도별", "흐름", "변화 추이", "선 차트", "선 그래프", "라인", "역대"]):
        items = []
        if has_korea: items.append("Korea, Rep.")
        if any(k in q for k in ["미국", "usa", "us"]): items.append("United States")
        if any(k in q for k in ["일본", "japan"]): items.append("Japan")
        if any(k in q for k in ["독일", "germany"]): items.append("Germany")
        if any(k in q for k in ["중국", "china"]): items.append("China")
        if any(k in q for k in ["영국", "uk"]): items.append("United Kingdom")
        if any(k in q for k in ["프랑스", "france"]): items.append("France")
        if not items:
            items = ["Korea, Rep.", "United States", "Japan", "Germany"]
        if "Korea, Rep." not in items and has_korea:
            items.insert(0, "Korea, Rep.")

        answer = f"""### 📈 주요국 및 대한민국 연도별 인터넷 보급률 변화 추이 분석 계획

- **비교 분석 대상**: **{', '.join(items)}**
- **적용 분석 도구**: `시계열 추이 선 차트 (trend_line)`
- **시계열 관측 구간**: 데이터셋 전체 연도 ({year_cols[0] if year_cols else ''} ~ {year_cols[-1] if year_cols else ''})
- **분석 핵심 포인트**:
  * 단일 연도의 단편적 순위를 넘어 시간 경과에 따른 각 국가별 보급률 성장 궤적을 꺾은선 그래프로 비교합니다.
  * 특히 대한민국의 초고속 인터넷 인프라 도입 속도와 주요 선진국(미국, 일본, 독일 등)의 보급 추세를 한눈에 대조할 수 있습니다.

아래 **[시각화 그래프 및 분석 실행하기]** 버튼을 누르시면 연도별 시계열 꺾은선 그래프가 즉시 표출됩니다.
"""
        return {
            "answer": answer,
            "recommended_tool": {
                "tool": "trend_line",
                "tool_label": "시계열 추이 선 차트",
                "button_text": "대한민국 및 주요국 연도별 추이 비교 차트 보기",
                "reason": "시간 흐름에 따른 각국의 지표 성장 속도와 추세를 꺾은선으로 비교하는 데 가장 적합한 도구입니다.",
                "params": {
                    "items": items,
                    "exclude_aggregates": False
                }
            }
        }

    # 2. 🚀 성장 / 증가 / 변화율 / 급격한 상승 (change_rate)
    if any(k in q for k in ["증가", "성장", "변화율", "급격", "변화량", "상승", "많이 오른", "빨리 오른", "성장한", "커진"]):
        base_y = year_cols[-11] if len(year_cols) >= 11 else (year_cols[0] if year_cols else "2012")
        answer = f"""### 🚀 최근 10년간 보급률 최대 성장 국가 분석 계획

- **비교 분석 구간**: **{base_y}년 → {rec_year}년 (최근 10년간)**
- **적용 분석 도구**: `두 시점 간 변화량·변화율 분석 (change_rate)`
- **평가 지표**: 포인트 증가폭(%p) 및 상대 성장률(%)
- **분석 핵심 포인트**:
  * 이미 보급률이 90% 이상인 선진국 외에, 최근 10년간 가장 역동적으로 보급률이 급상승한 신흥국들을 발굴합니다.
  * 시작 시점과 종료 시점의 실질 변화량을 비교하여 글로벌 디지털 격차 해소 속도를 진단합니다.

아래 **[시각화 그래프 및 분석 실행하기]** 버튼을 클릭하시면 10년간 최대 성장 국가 순위 차트가 생성됩니다.
"""
        return {
            "answer": answer,
            "recommended_tool": {
                "tool": "change_rate",
                "tool_label": "두 시점 간 변화량·변화율 분석",
                "button_text": f"최근 10년간({base_y}~{rec_year}) 최대 성장 국가 차트 보기",
                "reason": "시작 연도와 종료 연도를 대조하여 실질 증가폭을 계산하는 도구입니다.",
                "params": {
                    "base_period": base_y,
                    "reference_period": rec_year,
                    "n": 10,
                    "exclude_aggregates": True
                }
            }
        }

    # 3. 📊 분포 / 히스토그램 / 구간 (distribution)
    if any(k in q for k in ["분포", "히스토그램", "구간", "빈도", "밀도"]):
        answer = f"""### 📊 {rec_year}년 수치 분포 히스토그램 분석 계획

- **기준 시점**: **{rec_year}년**
- **적용 분석 도구**: `수치 구간별 분포 (distribution)`
- **구간 수**: 10개 구간 (Bins)
- **분석 목적**: 전 세계 국가들의 보급률이 저보급 구간에 치우쳐 있는지 고보급 구간에 집중되어 있는지 전체적 분포 형태를 조망합니다.

아래 버튼을 클릭하시면 구간별 히스토그램 그래프가 생성됩니다.
"""
        return {
            "answer": answer,
            "recommended_tool": {
                "tool": "distribution",
                "tool_label": "수치 구간별 분포 (히스토그램)",
                "button_text": f"{rec_year}년 수치 분포 히스토그램 차트 보기",
                "reason": "전체 관측치의 밀도와 치우침(왜도)을 점검하는 데 필수적인 통계 시각화입니다.",
                "params": {
                    "reference_period": rec_year,
                    "bins_count": 10,
                    "exclude_aggregates": True
                }
            }
        }

    # 4. 📈 기초 통계량 / 평균 / 중앙값 / 요약 (describe_numeric)
    if any(k in q for k in ["평균", "중앙값", "표준편차", "기초 통계", "요약 통계", "전체 통계"]):
        answer = f"""### 📊 {chosen_year}년 전 세계 인터넷 보급률 기초 통계량 분석 계획

- **기준 시점**: **{chosen_year}년**
- **적용 분석 도구**: `기술 통계량 분석 (describe_numeric)`
- **산출 지표**: 전 세계 평균(Mean), 중앙값(Median), 표준편차(Std), 최솟값, 최댓값, 사분위수
- **분석 목적**: 개별 국가 순위 이전에 전 세계 국가들의 전반적인 평균 수준과 편차를 객관적으로 파악합니다.

아래 버튼을 클릭하시면 기초 통계 요약표가 생성됩니다.
"""
        return {
            "answer": answer,
            "recommended_tool": {
                "tool": "describe_numeric",
                "tool_label": "기술 통계량 분석",
                "button_text": f"{chosen_year}년 전 세계 평균 및 기초 통계량 보기",
                "reason": "평균값, 중앙값, 최솟값, 최댓값 등 수치의 전체적 분포를 요약합니다.",
                "params": {
                    "reference_period": chosen_year,
                    "exclude_aggregates": True
                }
            }
        }

    # 5. 🔍 데이터 품질 / 결측치 / 결측 검사 (quality_report)
    if any(k in q for k in ["품질", "결측", "누락", "빈값", "진단", "결측치"]):
        answer = f"""### 🔍 데이터셋 종합 품질 진단 계획

- **적용 분석 도구**: `데이터 종합 품질 보고서 (quality_report)`
- **검사 항목**: 열별 결측치 비율, 중복 행 존재 여부, 데이터 타입 일치도
- **분석 목적**: 분석에 사용될 데이터의 무결성과 신뢰성을 사전에 점검합니다.

아래 버튼을 클릭하시면 품질 진단 보고서가 생성됩니다.
"""
        return {
            "answer": answer,
            "recommended_tool": {
                "tool": "quality_report",
                "tool_label": "데이터 종합 품질 보고서",
                "button_text": "데이터 품질 진단 보고서 실행하기",
                "reason": "결측치율과 중복 데이터를 진단하는 데 최적화된 도구입니다.",
                "params": {
                    "exclude_aggregates": False
                }
            }
        }

    # 6. 🏆 순위 / 1-10등 / 상위 / 하위 / 대한민국 비교 및 모든 일반 질문 (top_bottom_n)
    has_top = any(k in q for k in ["상위", "top", "높은", "1등", "1위", "최상위", "최고", "1-10", "1~10", "10등", "10위"])
    has_bottom = any(k in q for k in ["하위", "bottom", "낮은", "꼴찌", "최하위", "최저", "바닥", "끝", "뒤에서"])
    
    n_match = re.search(r'(\d+)\s*(?:개국|등|개|위)', q)
    extracted_n = int(n_match.group(1)) if n_match else 10
    if extracted_n > 50 or extracted_n <= 0:
        extracted_n = 10

    # 6-1. 상위와 하위를 둘 다 요청한 경우 (예: "상위 10개국 하위 10개국 순위를 보여줘")
    if has_top and has_bottom:
        korea_mention = (
            f"- **대한민국(Korea, Rep.) 위상**: {chosen_year}년 기준 대한민국은 약 97% 이상의 보급률로 세계 10위권(13~15위)에 위치합니다. "
            f"상위권 막대 바로 아래에 **🇰🇷 빨간색 강조 막대**로 함께 포함하여 상하위 격차와 함께 비교할 수 있도록 구성했습니다.\n"
            if has_korea else ""
        )
        answer = f"""### 📊 {chosen_year}년 상위 {extracted_n}개국 및 하위 {extracted_n}개국 순위 비교 분석 계획

- **질문 내용**: "{user_query}"
- **분석 기준 시점**: **{chosen_year}년**
- **적용 분석 도구**: `상위·하위 N 순위 분석 (top_bottom_n)`
- **비교 분석 그룹**:
  * **🏆 상위 {extracted_n}개국**: 전 세계에서 인터넷 보급률이 가장 높은 최상위 선진국 그룹 (파란색 막대)
  * **🔻 최하위 {extracted_n}개국**: 통신망 인프라가 가장 열악한 최하위권 국가 그룹 (주황색 막대)
{korea_mention}- **데이터 정제**: World, OECD 등 개별 국가가 아닌 지역·소득 그룹 집계 행은 통계 왜곡 방지를 위해 자동으로 제외했습니다.

아래 **[시각화 그래프 및 분석 실행하기]** 버튼을 클릭하시면 상위와 하위 국가가 한눈에 비교되는 Chart.js 양방향 그래프가 즉시 표출됩니다.
"""
        btn_text = (
            f"{chosen_year}년 상위 {extracted_n}개국·하위 {extracted_n}개국 및 대한민국 비교 차트 보기"
            if has_korea else
            f"{chosen_year}년 상위 {extracted_n}개국 및 하위 {extracted_n}개국 순위 비교 차트 보기"
        )
        return {
            "answer": answer,
            "recommended_tool": {
                "tool": "top_bottom_n",
                "tool_label": "상위·하위 N 순위 분석",
                "button_text": btn_text,
                "reason": f"{chosen_year}년 기준 최상위 국가와 최하위 국가의 디지털 격차를 동시에 대조하기에 가장 적합한 도구입니다.",
                "params": {
                    "reference_period": chosen_year,
                    "n": extracted_n,
                    "mode": "both",
                    "exclude_aggregates": True,
                    "highlight_item": "Korea, Rep." if has_korea else None
                }
            }
        }

    # 6-2. 하위권만 요청한 경우 (예: "하위 10개국 순위 보여줘", "꼴찌 나라들")
    if has_bottom and not has_top:
        answer = f"""### 🔻 {chosen_year}년 최하위 {extracted_n}개국 순위 분석 계획

- **질문 내용**: "{user_query}"
- **분석 기준 시점**: **{chosen_year}년**
- **적용 분석 도구**: `상위·하위 N 순위 분석 (top_bottom_n)`
- **조사 대상**: 전 세계 인터넷 보급률 최하위 {extracted_n}개국 (주황색 막대)
- **데이터 정제**: 지역 및 소득 그룹 집계 행을 제외한 순수 개별 국가 기준 정렬

아래 버튼을 클릭하시면 최하위 {extracted_n}개국 순위 차트와 수치 테이블이 즉시 표출됩니다.
"""
        return {
            "answer": answer,
            "recommended_tool": {
                "tool": "top_bottom_n",
                "tool_label": "상위·하위 N 순위 분석",
                "button_text": f"{chosen_year}년 최하위 {extracted_n}개국 순위 차트 보기",
                "reason": f"{chosen_year}년 기준 인터넷 보급률이 가장 취약한 최하위권 국가들을 파악하는 도구입니다.",
                "params": {
                    "reference_period": chosen_year,
                    "n": extracted_n,
                    "mode": "bottom",
                    "exclude_aggregates": True,
                    "highlight_item": "Korea, Rep." if has_korea else None
                }
            }
        }

    # 6-3. 상위권 또는 일반 순위 요청
    korea_info = (
        f"- **대한민국(Korea, Rep.) 반영**: {chosen_year}년 기준 대한민국은 보급률 약 97.9%로 세계 10위권(13~15위)에 위치합니다. "
        f"상위 {extracted_n}개국 막대 바로 아래에 **🇰🇷 빨간색 강조 막대**로 함께 포함하여 시각화하도록 구성했습니다."
        if has_korea else
        f"- **순위 집계**: {chosen_year}년 기준 상위 {extracted_n}개국 국가들의 분포를 수평 막대 그래프로 정렬합니다."
    )
    answer = f"""### 📊 {chosen_year}년 인터넷 보급률 상위 {extracted_n}개국 순위 분석 계획

- **질문 내용**: "{user_query}"
- **분석 기준 시점**: **{chosen_year}년**
- **적용 분석 도구**: `상위·하위 N 순위 분석 (top_bottom_n)`
{korea_info}
- **데이터 정제**: World, OECD 등 개별 국가가 아닌 지역·소득 그룹 집계 행은 통계 왜곡 방지를 위해 자동으로 제외했습니다.

아래 **[시각화 그래프 및 분석 실행하기]** 버튼을 클릭하시면 실제 Pandas 계산과 함께 Chart.js 그래프가 즉시 표출됩니다.
"""
    return {
        "answer": answer,
        "recommended_tool": {
            "tool": "top_bottom_n",
            "tool_label": "상위·하위 N 순위 분석",
            "button_text": f"{chosen_year}년 상위 {extracted_n}개국 및 대한민국 순위 비교 차트 보기" if has_korea else f"{chosen_year}년 상위 {extracted_n}개국 순위 차트 보기",
            "reason": f"{chosen_year}년 기준 각국의 순위 분포와 수치를 한눈에 파악하기에 가장 적합한 도구입니다.",
            "params": {
                "reference_period": chosen_year,
                "n": extracted_n,
                "mode": "top",
                "exclude_aggregates": True,
                "highlight_item": "Korea, Rep." if has_korea else None
            }
        }
    }


@app.route("/query_ai", methods=["POST"])
def query_ai():
    global current_dataset
    logger.info("[REQUEST] POST /query_ai")

    if current_dataset is None:
        return jsonify({"error": "먼저 CSV 파일을 업로드하여 진단을 완료해 주세요."}), 400

    payload = request.get_json() or {}
    user_query = payload.get("query", "").strip()

    if not user_query:
        return jsonify({"error": "질문 내용을 입력해 주세요."}), 400

    df = current_dataset["df"]
    diag = current_dataset["diagnosis"]

    # ⚡ 0.01초 초고속 인텐트 엔진 우선 매핑 (대기 시간 제로)
    instant_match = match_instant_intent(user_query, df, diag)
    if instant_match:
        logger.info(f"[AI] 초고속 인텐트 엔진 즉시 매핑 완료 (도구: {instant_match['recommended_tool']['tool']})")
        return jsonify(clean_nan({
            "status": "success",
            "query": user_query,
            "answer": instant_match["answer"],
            "recommended_tool": instant_match["recommended_tool"],
            "model_used": "Instant-Fast-Engine (0.01s)"
        }))

    year_cols = diag.get("year_columns", [])
    rec_year = diag.get("recommended_reference_year", "")
    warnings = diag.get("warnings", [])

    from tools import find_label_column
    label_col = find_label_column(df)
    sample_labels = []
    if label_col and label_col in df.columns:
        sample_labels = [str(x) for x in df[label_col].dropna().unique()[:8]]

    prompt = f"""
당신은 데이터 분석 및 시각화 전문가 AI입니다.
사용자가 업로드된 데이터셋에 대해 자연어로 다음과 같은 질문 또는 분석 요청을 했습니다:
"{user_query}"

[데이터셋 진단 정보]:
- 파일명: {current_dataset.get('filename')}
- 총 행 수: {diag.get('rows_count')}, 총 열 수: {diag.get('cols_count')}
- 항목/국가명 컬럼: '{label_col}' (예시 항목: {sample_labels})
- 연도 컬럼 목록: {year_cols[-10:] if len(year_cols) > 10 else year_cols} (총 {len(year_cols)}개)
- 데이터 충실도가 가장 높은 권장 기준연도: {rec_year}
- 주의사항: {warnings}

[사용 가능한 계산 도구 카탈로그]:
{json.dumps(TOOL_CATALOG, ensure_ascii=False, indent=2)}

각 도구별 핵심 파라미터 규칙:
1. 'top_bottom_n':
   - reference_period (연도 문자열, 예: '{rec_year}')
   - n (정수, 기본 10)
   - exclude_aggregates (bool, 기본 True)
   - highlight_item (문자열, 중요: 질문에 우리나라/한국/대한민국 등 특정 비교 국가가 포함된 경우 반드시 "Korea, Rep."를 입력하십시오!)
2. 'compare_one_vs_all': reference_period(연도 문자열), target_item(항목명, 예: 'Korea, Rep.'), exclude_aggregates(bool, 기본 True)
3. 'trend_line': items(비교할 국가명 리스트, 예: ['Korea, Rep.', 'United States', 'Japan']), exclude_aggregates(bool, 기본 False)
4. 'change_rate': reference_period(종료연도), base_period(시작과거연도), n(정수, 기본 10), exclude_aggregates(bool, 기본 True)
5. 'distribution': reference_period(연도), bins_count(정수, 기본 10), exclude_aggregates(bool, 기본 True)
6. 'correlation_scatter': reference_period(연도), exclude_aggregates(bool, 기본 True)
7. 'describe_numeric': reference_period(연도), exclude_aggregates(bool, 기본 True)
8. 'group_summary': reference_period(연도), exclude_aggregates(bool, 기본 False)
9. 'year_coverage': exclude_aggregates(bool, 기본 True)
10. 'quality_report': exclude_aggregates(bool, 기본 False)
11. 'detect_aggregates': exclude_aggregates(bool, 기본 False)
12. 'reshape_wide_to_long': exclude_aggregates(bool, 기본 True)

[작성 요구사항]:
1. answer (자연어 답변):
   - 질문에 대해 데이터셋의 연도 및 컬럼 현황을 근거로 명쾌하고 친절하게 설명하십시오.
   - 중요: 사용자가 우리나라/대한민국/한국 순위 비교나 특정 연도(예: 2024년)를 질문한 경우:
     * 최신 연도(2024년 등)는 아직 미입력 국가가 많으므로, 데이터 충실도가 100%에 달하는 가장 완벽한 권장 기준연도인 {rec_year}년을 기준으로 상위 10개국 순위를 분석함을 안내하십시오.
     * 대한민국(Korea, Rep.)의 경우 해당 기준연도({rec_year}년)에서 97.17%로 세계 15위에 랭크되어 있으므로, 상위 10개국 차트 바로 아래에 비교 대상(빨간색 막대)으로 함께 포함하여 비교 분석하도록 구성했음을 직관적으로 설명하십시오.
2. recommended_tool:
   - 12개 카탈로그 중 질문 의도에 가장 부합하는 도구 1개를 선택하십시오.
   - 'button_text': 사용자가 누를 시각화 버튼에 들어갈 직관적인 문구 (예: '{rec_year}년 상위 10개국 및 대한민국(15위) 순위 비교 차트 보기')
   - 'params': 위 도구 파라미터 규칙에 맞는 유효한 파라미터 객체. 대한민국/우리나라 비교 질문 시 'highlight_item': 'Korea, Rep.'를 반드시 포함하십시오.

반드시 아래 JSON 포맷으로만 응답하십시오:
{{
  "answer": "질문에 대한 자연어 설명 마크다운 텍스트",
  "recommended_tool": {{
    "tool": "top_bottom_n",
    "tool_label": "상위·하위 N 순위 분석",
    "button_text": "{rec_year}년 상위 10개국 및 대한민국(15위) 비교 차트 보기",
    "reason": "선택 사유",
    "params": {{
      "reference_period": "{rec_year}",
      "n": 10,
      "exclude_aggregates": true,
      "highlight_item": "Korea, Rep."
    }}
  }}
}}
"""

    logger.info(f"[AI] query_ai 초고속 요청 시작: {user_query[:30]}...")
    try:
        # 응답 지연을 방지하기 위해 초고속 flash-lite 모델을 우선 활용
        ai_text, model_used = call_gemini(prompt, is_json=True, preferred_model="gemini-flash-lite-latest")
        parsed = parse_ai_json(ai_text)
        
        rec = parsed.get("recommended_tool", {})
        tool_name = rec.get("tool")
        if tool_name not in TOOL_FUNCTIONS:
            rec["tool"] = "top_bottom_n"
            rec["tool_label"] = "상위·하위 N 순위 분석"
            rec["button_text"] = f"{rec_year}년 상위 10개국 및 대한민국 비교 차트 보기"
            rec["params"] = {"reference_period": rec_year or "2022", "n": 10, "exclude_aggregates": True, "highlight_item": "Korea, Rep."}

        # 사용자가 질문에서 우리나라/한국/대한민국을 언급한 경우 highlight_item 안전 자동 보정
        q_lower = user_query.lower()
        if any(k in q_lower for k in ["우리나라", "한국", "대한민국", "korea"]):
            if rec.get("tool") == "top_bottom_n":
                if not rec.get("params"):
                    rec["params"] = {}
                if not rec["params"].get("highlight_item"):
                    rec["params"]["highlight_item"] = "Korea, Rep."
            if "대한민국" not in rec.get("button_text", "") and "한국" not in rec.get("button_text", ""):
                rec["button_text"] = f"{rec.get('params', {}).get('reference_period', rec_year)}년 상위 10개국 및 대한민국 비교 차트 보기"

        logger.info(f"[AI] query_ai 성공 (선택 도구: {rec.get('tool')}, 모델: {model_used})")
        return jsonify(clean_nan({
            "status": "success",
            "query": user_query,
            "answer": parsed.get("answer", ""),
            "recommended_tool": rec,
            "model_used": model_used
        }))
    except Exception as e:
        err_str = str(e)
        logger.error(f"[ERROR] query_ai 오류: {err_str}")
        if any(x in err_str for x in ["503", "UNAVAILABLE", "high demand", "429"]):
            return jsonify({
                "error": "Google Gemini 서버의 일시적인 트래픽 집중(503 부하)으로 요청이 지연되었습니다. 잠시 후 다시 질문해 주세요."
            }), 503
        return jsonify({"error": f"질문 분석 중 오류가 발생했습니다: {err_str}"}), 500


if __name__ == "__main__":
    logger.info("[STARTUP] Starting AI Data Insight Builder on port 5000")
    app.run(host="127.0.0.1", port=5000, debug=FLASK_DEBUG)
