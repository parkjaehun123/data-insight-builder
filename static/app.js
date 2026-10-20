/**
 * AI Data Insight Builder - Vanilla JavaScript Frontend
 * 절대 규칙 준수: 콘솔 로깅 규격, 보안 키 미노출, Chart.js 동적 렌더링
 */

// 전역 상태 변수
let currentDiagnosis = null;
let currentChart = null;
let currentNlChart = null;
let latestAnalysisResult = null;
let latestReportMarkdown = "";
let latestNlRecommendation = null;

// ==============================================================================
// 전역 401(세션 만료/미인증) 인터셉터
// ==============================================================================
const _originalFetch = window.fetch;
window.fetch = async (...args) => {
  const response = await _originalFetch(...args);
  if (response.status === 401) {
    try {
      const cloned = response.clone();
      const errData = await cloned.json();
      if (errData && errData.auth_required) {
        showAuthOverlay("접속 세션이 만료되었습니다. 다시 비밀번호를 입력해 주세요.");
      }
    } catch (e) {
      // JSON 파싱 실패 시 무시
    }
  }
  return response;
};

// ==============================================================================
// 1. 초기화 및 서버 헬스체크
// ==============================================================================
document.addEventListener("DOMContentLoaded", () => {
  console.log("[PAGE] 페이지 로드 완료");
  initAuth();
  registerServiceWorker();
  setupPwaInstall();
  checkServerHealth();
  setupEventListeners();
  populateManualTools();
});

async function checkServerHealth() {
  const badge = document.getElementById("apiStatusBadge");
  try {
    const res = await fetch("/health");
    const data = await res.json();
    if (data.status === "ok") {
      if (data.gemini_api_key_configured) {
        badge.className = "badge badge-success";
        badge.textContent = `🟢 Gemini 연결됨 (${data.gemini_model})`;
        badge.title = "Gemini API가 정상적으로 연결되었습니다.";
      } else {
        badge.className = "badge badge-warning";
        badge.textContent = "🟡 API Key 미설정 (Vercel 환경변수 확인 필요)";
        badge.title = "Vercel 대시보드의 Environment Variables에 GEMINI_API_KEY를 등록해 주세요.";
      }
    }
  } catch (err) {
    console.error("[ERROR] 서버 헬스체크 실패:", err);
    badge.className = "badge badge-danger";
    badge.textContent = "🔴 서버 연결 실패";
  }
}

// 12개 도구 메타데이터 정의 (설명, 차트 유형, 필요 파라미터 필드)
const MANUAL_TOOL_DEFS = {
  top_bottom_n: {
    label: "상위·하위 N 순위 분석 (top_bottom_n)",
    description: "선택한 기준 연도에서 지표가 가장 높은 상위 N개와 가장 낮은 하위 N개 항목을 순위별로 비교합니다.",
    chartType: "수평 막대 차트 (상위 파란색, 하위 주황색)",
    fields: ["ref", "n", "excludeAgg"]
  },
  trend_line: {
    label: "시계열 추이 선 차트 (trend_line)",
    description: "관심 항목들(예: 한국, 미국 등)의 연도별 시계열 변화 흐름을 꺾은선으로 비교합니다.",
    chartType: "다중 꺾은선(Line) 차트",
    fields: ["trendItems", "excludeAgg"]
  },
  compare_one_vs_all: {
    label: "단일 항목 vs 전체 평균 강조 비교 (compare_one_vs_all)",
    description: "특정 관심 항목(예: 한국)의 수치를 전체 평균, 중앙값, 최고값과 직접 대조하여 위치를 파악합니다.",
    chartType: "비교 막대 차트 (한국 강조 색상)",
    fields: ["ref", "targetItem", "excludeAgg"]
  },
  change_rate: {
    label: "두 시점 간 변화량·변화율 분석 (change_rate)",
    description: "과거 시점과 기준 연도 사이의 증가폭(%p) 및 성장률(%)을 계산하여 가장 크게 성장한 항목을 찾습니다.",
    chartType: "변화량 수평 막대 차트",
    fields: ["ref", "base", "n", "excludeAgg"]
  },
  distribution: {
    label: "수치 구간별 분포 히스토그램 (distribution)",
    description: "기준 연도의 전체 수치들이 어느 구간에 밀집되어 있는지 빈도수 분포를 분석합니다.",
    chartType: "히스토그램 막대 차트",
    fields: ["ref", "bins", "excludeAgg"]
  },
  correlation_scatter: {
    label: "상관관계 산점도 분석 (correlation_scatter)",
    description: "두 시점 또는 두 변수 간 피어슨 상관계수(r)와 산점도를 분석합니다. (상관!=인과 주의)",
    chartType: "2D 산점도 (Scatter) 차트",
    fields: ["ref", "excludeAgg"]
  },
  describe_numeric: {
    label: "기술 통계량 분석 (describe_numeric)",
    description: "기준 연도 수치의 평균, 중앙값, 표준편차, 최댓값, 사분위수 등 핵심 통계량을 한눈에 요약합니다.",
    chartType: "통계 지표 요약 표 및 막대 차트",
    fields: ["ref", "excludeAgg"]
  },
  group_summary: {
    label: "그룹·구간별 요약 집계 (group_summary)",
    description: "데이터를 수치 구간 또는 그룹별로 묶어 평균과 개수를 집계합니다.",
    chartType: "그룹별 평균 막대 차트",
    fields: ["ref", "excludeAgg"]
  },
  year_coverage: {
    label: "연도별 데이터 충실도 분석 (year_coverage)",
    description: "각 연도별로 결측치 없이 유효하게 입력된 데이터의 비율을 전체 연도에 걸쳐 전수 검사합니다.",
    chartType: "연도별 충실도(%) 꺾은선 차트",
    fields: ["excludeAgg"]
  },
  detect_aggregates: {
    label: "집계 행 탐지 (detect_aggregates)",
    description: "전 세계(World), OECD, 고소득국 등 개별 국가가 아닌 지역/소득 묶음 집계 행을 전수 추출합니다.",
    chartType: "집계 행 목록 상세 테이블",
    fields: []
  },
  quality_report: {
    label: "데이터 종합 품질 보고서 (quality_report)",
    description: "열별 결측치율, 행 중복, 비어있는 열, 비표준 헤더 등을 포괄적으로 검증합니다.",
    chartType: "결측치 비율 막대 차트 및 품질 진단표",
    fields: []
  },
  reshape_wide_to_long: {
    label: "와이드 → 롱 포맷 변환 (reshape_wide_to_long)",
    description: "가로로 나열된 연도 열들을 'Year', 'Value' 행으로 접어 시계열 DB 분석용 표로 변환합니다.",
    chartType: "변환된 롱 포맷 데이터 미리보기 테이블",
    fields: ["excludeAgg"]
  }
};

// 12개 도구 목록 수동 셀렉트박스 채우기
function populateManualTools() {
  const select = document.getElementById("manualToolSelect");
  select.innerHTML = Object.keys(MANUAL_TOOL_DEFS).map(key => {
    return `<option value="${key}">${MANUAL_TOOL_DEFS[key].label}</option>`;
  }).join("");

  select.addEventListener("change", onManualToolSelectChange);
  onManualToolSelectChange();
}

function onManualToolSelectChange() {
  const toolKey = document.getElementById("manualToolSelect").value;
  const def = MANUAL_TOOL_DEFS[toolKey];
  if (!def) return;

  const banner = document.getElementById("manualToolInfoBanner");
  banner.innerHTML = `
    <div><strong>💡 도구 목적:</strong> ${def.description}</div>
    <div style="margin-top: 4px;"><strong>📊 시각화 방식:</strong> ${def.chartType}</div>
  `;

  const fields = def.fields || [];
  document.getElementById("groupRefPeriod").style.display = fields.includes("ref") ? "block" : "none";
  document.getElementById("groupBasePeriod").style.display = fields.includes("base") ? "block" : "none";
  document.getElementById("groupN").style.display = fields.includes("n") ? "block" : "none";
  document.getElementById("groupTargetItem").style.display = fields.includes("targetItem") ? "block" : "none";
  document.getElementById("groupTrendItems").style.display = fields.includes("trendItems") ? "block" : "none";
  document.getElementById("groupBins").style.display = fields.includes("bins") ? "block" : "none";
  document.getElementById("groupExcludeAgg").style.display = fields.includes("excludeAgg") ? "block" : "none";
}

// ==============================================================================
// 2. 이벤트 리스너 등록 (드래그 앤 드롭, 버튼 클릭)
// ==============================================================================
function setupEventListeners() {
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("csvFileInput");

  // 드롭존 클릭 시 파일 선택창 열기
  dropzone.addEventListener("click", () => fileInput.click());

  // 드래그 앤 드롭 효과
  dropzone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropzone.classList.add("dragover");
  });

  dropzone.addEventListener("dragleave", () => {
    dropzone.classList.remove("dragover");
  });

  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragover");
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      handleFileUpload(e.dataTransfer.files[0]);
    }
  });

  fileInput.addEventListener("change", (e) => {
    if (e.target.files && e.target.files.length > 0) {
      handleFileUpload(e.target.files[0]);
    }
  });

  // 분석 모드 탭 전환
  const tabBtnAi = document.getElementById("tabBtnAi");
  const tabBtnManual = document.getElementById("tabBtnManual");
  const aiTab = document.getElementById("aiSuggestTabContent");
  const manualTab = document.getElementById("manualToolTabContent");

  tabBtnAi.addEventListener("click", () => {
    tabBtnAi.classList.add("active");
    tabBtnManual.classList.remove("active");
    aiTab.style.display = "block";
    manualTab.style.display = "none";
  });

  tabBtnManual.addEventListener("click", () => {
    tabBtnManual.classList.add("active");
    tabBtnAi.classList.remove("active");
    manualTab.style.display = "block";
    aiTab.style.display = "none";
  });

  // 빠른 선택 칩 버튼 클릭
  document.querySelectorAll("#targetItemChips .chip-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.getElementById("manualTargetItem").value = btn.getAttribute("data-val");
    });
  });

  // Gemini 제안 요청 버튼
  document.getElementById("btnGetSuggestions").addEventListener("click", requestSuggestions);

  // 수동 도구 직접 실행 버튼
  document.getElementById("btnRunManual").addEventListener("click", () => {
    const tool = document.getElementById("manualToolSelect").value;
    const def = MANUAL_TOOL_DEFS[tool];
    const fields = def ? def.fields : [];

    const params = {};
    if (fields.includes("ref")) {
      params.reference_period = document.getElementById("manualRefYear").value;
    }
    if (fields.includes("base")) {
      params.base_period = document.getElementById("manualBaseYear").value;
    }
    if (fields.includes("n")) {
      params.n = parseInt(document.getElementById("manualN").value, 10) || 10;
    }
    if (fields.includes("targetItem")) {
      params.target_item = document.getElementById("manualTargetItem").value.trim() || "Korea, Rep.";
    }
    if (fields.includes("trendItems")) {
      const raw = document.getElementById("manualTrendItems").value;
      params.items = raw.split(",").map(s => s.trim()).filter(Boolean);
    }
    if (fields.includes("bins")) {
      params.bins_count = parseInt(document.getElementById("manualBins").value, 10) || 10;
    }
    if (fields.includes("excludeAgg")) {
      params.exclude_aggregates = document.getElementById("manualExcludeAgg").checked;
    }

    console.log(`[RUN] 수동 설정 도구 실행 (도구: ${tool}, 파라미터:`, params, ")");
    executeTool(tool, params);
  });

  // 모드 라디오 카드 클릭 스타일 토글
  document.querySelectorAll(".radio-card").forEach(card => {
    card.addEventListener("click", () => {
      document.querySelectorAll(".radio-card").forEach(c => c.classList.remove("active"));
      card.classList.add("active");
      const radio = card.querySelector("input[type='radio']");
      if (radio) radio.checked = true;
    });
  });

  // 보고서 생성 버튼
  document.getElementById("btnGenerateReport").addEventListener("click", requestReport);

  // 보고서 클립보드 복사 버튼
  document.getElementById("btnCopyReport").addEventListener("click", () => {
    if (!latestReportMarkdown) return;
    navigator.clipboard.writeText(latestReportMarkdown).then(() => {
      alert("보고서 본문이 클립보드에 복사되었습니다.");
    });
  });

  // 추천 후속 질문 말풍선 칩 클릭
  document.querySelectorAll("#sqChipsContainer .question-chip").forEach(chip => {
    chip.addEventListener("click", () => {
      const q = chip.getAttribute("data-q");
      if (q) {
        document.getElementById("followupInput").value = q;
        sendFollowupQuestion(q);
      }
    });
  });

  // 후속 질문 전송 버튼 클릭
  document.getElementById("btnSendFollowup").addEventListener("click", () => {
    const input = document.getElementById("followupInput");
    const q = input.value.trim();
    if (q) {
      sendFollowupQuestion(q);
    }
  });

  // 후속 질문 엔터키 지원
  document.getElementById("followupInput").addEventListener("keypress", (e) => {
    if (e.key === "Enter") {
      const q = e.target.value.trim();
      if (q) {
        sendFollowupQuestion(q);
      }
    }
  });

  // 💡 2.0 자연어 질문 예시 칩 클릭
  document.querySelectorAll("#nlExampleChips .question-chip").forEach(chip => {
    chip.addEventListener("click", () => {
      const q = chip.getAttribute("data-q");
      if (q) {
        document.getElementById("nlQueryInput").value = q;
        handleNlQuery(q);
      }
    });
  });

  // 자연어 질문 분석 버튼 클릭
  document.getElementById("btnSendNlQuery").addEventListener("click", () => {
    const q = document.getElementById("nlQueryInput").value.trim();
    if (q) {
      handleNlQuery(q);
    }
  });

  // 자연어 질문 엔터키 지원
  document.getElementById("nlQueryInput").addEventListener("keypress", (e) => {
    if (e.key === "Enter") {
      const q = e.target.value.trim();
      if (q) {
        handleNlQuery(q);
      }
    }
  });

  // 자연어 맞춤 시각화 그래프 실행 버튼 클릭 (질문 바로 아래 인라인 차트 표출)
  document.getElementById("btnNlExecuteViz").addEventListener("click", () => {
    if (!latestNlRecommendation || !latestNlRecommendation.tool) return;
    console.log("[VIZ] 자연어 질문 기반 시각화 실행:", latestNlRecommendation);
    executeNlInlineViz(latestNlRecommendation.tool, latestNlRecommendation.params, latestNlRecommendation.button_text);
  });

  // 질문 바로 아래 인라인 영역에서 '4단계 AI 보고서 바로 작성하기' 클릭
  const btnReport = document.getElementById("btnNlTriggerReport");
  if (btnReport) {
    btnReport.addEventListener("click", () => {
      const explainSection = document.getElementById("explainSection");
      explainSection.style.display = "block";
      explainSection.scrollIntoView({ behavior: "smooth" });
      requestReport();
    });
  }
}

// ==============================================================================
// 3. 파일 업로드 및 정밀 진단 (/inspect)
// ==============================================================================
async function handleFileUpload(file) {
  if (!file) return;

  if (!file.name.toLowerCase().endsWith(".csv")) {
    alert("CSV 파일만 업로드할 수 있습니다 (.csv).");
    return;
  }

  console.log(`[UPLOAD] 파일 선택 (파일명: ${file.name}, 크기: ${(file.size / 1024).toFixed(1)} KB)`);

  const loader = document.getElementById("uploadLoader");
  const diagArea = document.getElementById("diagnosisArea");
  loader.style.display = "flex";
  diagArea.style.display = "none";

  const formData = new FormData();
  formData.append("file", file);

  console.log("[INSPECT] 진단 요청 시작");

  try {
    const res = await fetch("/inspect", {
      method: "POST",
      body: formData
    });

    const data = await res.json();
    loader.style.display = "none";

    if (!res.ok) {
      console.error("[ERROR] 진단 요청 실패:", data.error);
      alert(data.error || "파일 진단 중 오류가 발생했습니다.");
      return;
    }

    currentDiagnosis = data.diagnosis;
    const warningsCount = (currentDiagnosis.warnings || []).length;
    console.log(`[INSPECT] 응답 수신 (행 수: ${currentDiagnosis.rows_count}, 열 수: ${currentDiagnosis.cols_count}, 경고 수: ${warningsCount})`);

    // 진단 결과 UI 렌더링
    renderDiagnosisUI(data);

    // 다음 섹션 노출
    document.getElementById("suggestSection").style.display = "block";
    document.getElementById("suggestSection").scrollIntoView({ behavior: "smooth" });

  } catch (err) {
    loader.style.display = "none";
    console.error("[ERROR] 진단 통신 오류:", err);
    alert("서버와 통신하는 중 오류가 발생했습니다.");
  }
}

function renderDiagnosisUI(data) {
  const diag = data.diagnosis;
  const metricsGrid = document.getElementById("metricsGrid");

  metricsGrid.innerHTML = `
    <div class="metric-card">
      <div class="metric-label">파일명 / 크기</div>
      <div class="metric-value" style="font-size: 1rem;">${data.file_name}</div>
      <small style="color: var(--text-muted);">${data.file_size_kb} KB</small>
    </div>
    <div class="metric-card">
      <div class="metric-label">인코딩 / 헤더</div>
      <div class="metric-value metric-highlight">${data.encoding}</div>
      <small style="color: var(--text-muted);">${data.skiprows}줄 건너뜀 (헤더 자동보정)</small>
    </div>
    <div class="metric-card">
      <div class="metric-label">행 x 열 개수</div>
      <div class="metric-value">${diag.rows_count.toLocaleString()} 행 x ${diag.cols_count} 열</div>
      <small style="color: var(--text-muted);">연도 열: ${diag.year_columns.length}개</small>
    </div>
    <div class="metric-card">
      <div class="metric-label">권장 기준연도</div>
      <div class="metric-value metric-highlight" style="color: var(--success);">${diag.recommended_reference_year || "없음"}</div>
      <small style="color: var(--text-muted);">가장 데이터가 충실한 최신 연도</small>
    </div>
  `;

  // 수동 기준연도 및 비교연도 셀렉트박스 채우기
  const refSelect = document.getElementById("manualRefYear");
  const baseSelect = document.getElementById("manualBaseYear");

  if (diag.year_columns && diag.year_columns.length > 0) {
    const descYears = [...diag.year_columns].reverse();
    refSelect.innerHTML = descYears.map(y => `<option value="${y}">${y}년</option>`).join("");
    if (diag.recommended_reference_year) {
      refSelect.value = diag.recommended_reference_year;
    }

    baseSelect.innerHTML = diag.year_columns.map(y => `<option value="${y}">${y}년</option>`).join("");
    const recYear = parseInt(diag.recommended_reference_year || descYears[0], 10);
    const candidatePast = String(recYear - 10);
    if (diag.year_columns.includes(candidatePast)) {
      baseSelect.value = candidatePast;
    } else {
      baseSelect.value = diag.year_columns[0];
    }
  }

  // 경고 상자
  const warnBox = document.getElementById("warningsBox");
  const warnList = document.getElementById("warningsList");
  if (diag.warnings && diag.warnings.length > 0) {
    warnList.innerHTML = diag.warnings.map(w => `<li>${w}</li>`).join("");
    warnBox.style.display = "block";
  } else {
    warnBox.style.display = "none";
  }

  // 미리보기 테이블 렌더링
  renderPreviewTable(diag.preview_rows, diag.columns);
  document.getElementById("diagnosisArea").style.display = "block";
}

function renderPreviewTable(rows, columns) {
  const container = document.getElementById("previewTableContainer");
  if (!rows || rows.length === 0) {
    container.innerHTML = "<p>미리보기 데이터가 없습니다.</p>";
    return;
  }

  // 앞쪽 10개 열만 미리보기
  const displayCols = columns.slice(0, 10);
  let html = "<table><thead><tr>";
  displayCols.forEach(col => {
    html += `<th>${col}</th>`;
  });
  html += "</tr></thead><tbody>";

  rows.forEach(r => {
    html += "<tr>";
    displayCols.forEach(col => {
      const val = r[col] !== null && r[col] !== undefined ? r[col] : "<span style='color:#cbd5e1;'>(빈값)</span>";
      html += `<td>${val}</td>`;
    });
    html += "</tr>";
  });
  html += "</tbody></table>";
  container.innerHTML = html;
}

// ==============================================================================
// 4. Gemini AI 분석 제안 요청 (/suggest)
// ==============================================================================
async function requestSuggestions() {
  const btn = document.getElementById("btnGetSuggestions");
  const loader = document.getElementById("suggestLoader");
  const container = document.getElementById("suggestionsContainer");

  btn.disabled = true;
  loader.style.display = "flex";
  container.innerHTML = "";

  console.log("[SUGGEST] 제안 요청 시작");

  try {
    const res = await fetch("/suggest", { method: "POST" });
    const data = await res.json();
    loader.style.display = "none";
    btn.disabled = false;

    if (!res.ok) {
      console.error("[ERROR] 제안 요청 실패:", data.error);
      alert(data.error || "분석 제안을 불러오지 못했습니다.");
      return;
    }

    const suggestions = data.suggestions || [];
    console.log(`[SUGGEST] 응답 수신 (제안 개수: ${suggestions.length})`);

    if (suggestions.length === 0) {
      container.innerHTML = "<p>제안된 분석 도구가 없습니다. 수동 도구 선택기를 이용해 주세요.</p>";
      return;
    }

    // 제안 카드 렌더링
    container.innerHTML = suggestions.map((s, idx) => `
      <div class="suggestion-card">
        <div>
          <h4>${idx + 1}. ${s.tool_label || s.tool}</h4>
          <p class="suggestion-why">${s.why}</p>
          <div class="suggestion-caution">⚠️ <strong>주의:</strong> ${s.caution}</div>
        </div>
        <div>
          <button class="btn btn-primary" style="width: 100%;" onclick="executeSuggestedAnalysis(${idx})">
            🚀 이 분석 실행하기
          </button>
        </div>
      </div>
    `).join("");

    // 전역에 제안 목록 임시 보관
    window._latestSuggestions = suggestions;

  } catch (err) {
    loader.style.display = "none";
    btn.disabled = false;
    console.error("[ERROR] 제안 통신 오류:", err);
    alert("Gemini 제안 통신 중 오류가 발생했습니다.");
  }
}

// 제안 카드 버튼 클릭 핸들러
window.executeSuggestedAnalysis = function(index) {
  if (!window._latestSuggestions || !window._latestSuggestions[index]) return;
  const item = window._latestSuggestions[index];
  executeTool(item.tool, item.params);
};

// ==============================================================================
// 5. 도구 실제 계산 실행 (/run) & Chart.js 렌더링
// ==============================================================================
async function executeTool(toolName, params) {
  console.log(`[RUN] 도구 실행 시작 (도구명: ${toolName}, 기준시점: ${params.reference_period || "없음"})`);

  const resultSection = document.getElementById("resultSection");
  const runLoader = document.getElementById("runLoader");
  const resultContent = document.getElementById("resultContent");

  resultSection.style.display = "block";
  runLoader.style.display = "flex";
  resultContent.style.display = "none";
  resultSection.scrollIntoView({ behavior: "smooth" });

  try {
    const res = await fetch("/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool: toolName, params: params })
    });

    const data = await res.json();
    runLoader.style.display = "none";

    if (!res.ok) {
      console.error("[ERROR] 도구 실행 실패:", data.error);
      alert(data.error || "도구 실행 중 오류가 발생했습니다.");
      return;
    }

    latestAnalysisResult = data;
    console.log("[RUN] 결과 수신 완료");

    // 실행 메타정보 렌더링
    renderRunMetadata(data);

    // 차트 렌더링
    if (data.result && data.result.chart_data) {
      renderChart(data.result.chart_data);
    } else {
      hideChart();
    }

    // 결과 상세 테이블 렌더링
    renderResultDetailTable(data.result);

    resultContent.style.display = "block";

    // 4단계(해석 섹션) 노출
    document.getElementById("explainSection").style.display = "block";

  } catch (err) {
    runLoader.style.display = "none";
    console.error("[ERROR] 도구 실행 통신 오류:", err);
    alert("도구 실행 중 네트워크 통신 오류가 발생했습니다.");
  }
}

function renderRunMetadata(data) {
  const r = data.result;
  document.getElementById("metaToolName").textContent = data.tool_label || data.tool;
  document.getElementById("metaRefPeriod").textContent = r.reference_period || "-";
  document.getElementById("metaUsedRows").textContent = `${r.used_rows_count}개`;
  document.getElementById("metaExcludedRows").textContent = `${r.excluded_rows_count}개 (${r.excluded_reason || "사유 없음"})`;
  document.getElementById("metaUnit").textContent = r.unit || "-";
  document.getElementById("resultCautionText").textContent = `⚠️ 분석 시 주의사항: ${r.caution || "없음"}`;
}

// Chart.js 렌더링
function renderChart(chartData) {
  const ctx = document.getElementById("analysisChart").getContext("2d");

  // 이전 차트 인스턴스 파괴
  if (currentChart) {
    currentChart.destroy();
  }

  const chartType = chartData.type || "bar";
  const config = {
    type: chartType,
    data: {
      labels: chartData.labels || [],
      datasets: chartData.datasets || []
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      indexAxis: chartData.indexAxis || "x",
      plugins: {
        legend: {
          display: chartType !== "bar" || (chartData.datasets && chartData.datasets.length > 1),
          position: "top"
        }
      }
    }
  };

  currentChart = new Chart(ctx, config);
  console.log(`[CHART] 차트 렌더 완료 (차트 종류: ${chartType})`);
}

function hideChart() {
  if (currentChart) {
    currentChart.destroy();
    currentChart = null;
  }
}

// 결과 상세 표 렌더링 (도구별 적합한 테이블 표출 - 대상 컨테이너 지정 가능)
function renderResultDetailTable(result, containerId = "resultTableContainer") {
  const container = document.getElementById(containerId);
  if (!container) return;
  let records = [];
  if (result.combined_n && result.combined_n.length > 0) {
    records = result.combined_n;
  } else if (result.mode === "bottom" && result.bottom_n) {
    records = result.bottom_n;
  } else if (result.top_n) {
    records = result.top_n;
  } else if (result.top_gainers) {
    records = result.top_gainers;
  } else if (result.summary_table) {
    records = result.summary_table;
  } else if (result.bins_summary) {
    records = result.bins_summary;
  } else if (result.coverage_table) {
    records = result.coverage_table.slice(-10); // 최근 10년치
  } else if (result.preview) {
    records = result.preview;
  }

  if (records.length === 0) {
    container.innerHTML = "";
    return;
  }

  const headers = Object.keys(records[0]);
  let html = "<h4 style='margin-bottom: 8px;'>📊 계산 상세 수치 데이터</h4><table><thead><tr>";
  headers.forEach(h => html += `<th>${h}</th>`);
  html += "</tr></thead><tbody>";

  records.forEach(row => {
    html += "<tr>";
    headers.forEach(h => {
      html += `<td>${row[h]}</td>`;
    });
    html += "</tr>";
  });
  html += "</tbody></table>";
  container.innerHTML = html;
}

// 💡 질문 바로 아래 인라인 Chart.js 렌더링
function renderNlInlineChart(chartData) {
  const canvas = document.getElementById("nlInlineChartCanvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");

  if (currentNlChart) {
    currentNlChart.destroy();
    currentNlChart = null;
  }

  const chartType = chartData.type || "bar";
  const config = {
    type: chartType,
    data: {
      labels: chartData.labels || [],
      datasets: chartData.datasets || []
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      indexAxis: chartData.indexAxis || "x",
      plugins: {
        legend: {
          display: chartType !== "bar" || (chartData.datasets && chartData.datasets.length > 1),
          position: "top"
        }
      }
    }
  };

  currentNlChart = new Chart(ctx, config);
  console.log(`[CHART] 인라인 맞춤 차트 렌더 완료 (차트 종류: ${chartType})`);
}

// 💡 질문 바로 아래 인라인 시각화 실행 (/run)
async function executeNlInlineViz(toolName, params, buttonText) {
  console.log(`[NL_VIZ] 질문 바로 아래 인라인 시각화 실행 (도구: ${toolName})`);

  const inlineArea = document.getElementById("nlInlineVizArea");
  const inlineLoader = document.getElementById("nlInlineLoader");
  const inlineContent = document.getElementById("nlInlineContent");

  if (!inlineArea) return;
  inlineArea.style.display = "block";
  inlineLoader.style.display = "flex";
  inlineContent.style.display = "none";

  if (buttonText) {
    const cleanTitle = buttonText.replace(/^[📊📈🚀🏆✨\s]+/, "");
    document.getElementById("nlInlineVizTitle").textContent = cleanTitle;
  }

  try {
    const res = await fetch("/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool: toolName, params: params })
    });

    const data = await res.json();
    inlineLoader.style.display = "none";

    if (!res.ok) {
      console.error("[ERROR] 인라인 시각화 실행 실패:", data.error);
      alert(data.error || "도구 실행 중 오류가 발생했습니다.");
      return;
    }

    latestAnalysisResult = data;
    console.log("[NL_VIZ] 인라인 결과 수신 완료");

    // 메타 정보 렌더링
    const r = data.result || {};
    document.getElementById("nlMetaToolName").textContent = data.tool_label || data.tool;
    document.getElementById("nlMetaRefPeriod").textContent = r.reference_period || "-";
    document.getElementById("nlMetaUsedRows").textContent = `${r.used_rows_count || 0}개`;
    document.getElementById("nlMetaExcludedRows").textContent = `${r.excluded_rows_count || 0}개 (${r.excluded_reason || "사유 없음"})`;
    document.getElementById("nlMetaUnit").textContent = r.unit || "-";

    const cautionEl = document.getElementById("nlResultCautionText");
    if (r.caution) {
      cautionEl.textContent = `⚠️ 분석 시 주의사항: ${r.caution}`;
      cautionEl.style.display = "block";
    } else {
      cautionEl.style.display = "none";
    }

    // 인라인 차트 렌더링
    if (r.chart_data) {
      renderNlInlineChart(r.chart_data);
    }

    // 인라인 상세 표 렌더링
    renderResultDetailTable(r, "nlInlineTableContainer");

    inlineContent.style.display = "block";

    // 4단계 보고서 섹션도 활성화
    document.getElementById("explainSection").style.display = "block";

  } catch (err) {
    inlineLoader.style.display = "none";
    console.error("[ERROR] 인라인 시각화 통신 오류:", err);
    alert("시각화 그래프 생성 중 통신 오류가 발생했습니다.");
  }
}

// ==============================================================================
// 6. Gemini AI 인사이트 해석 보고서 생성 (/explain)
// ==============================================================================
async function requestReport() {
  const modeRadios = document.getElementsByName("explainMode");
  let selectedMode = "B";
  for (const r of modeRadios) {
    if (r.checked) selectedMode = r.value;
  }

  console.log(`[EXPLAIN] 해석 요청 시작 (모드: ${selectedMode})`);

  const btn = document.getElementById("btnGenerateReport");
  const loader = document.getElementById("explainLoader");
  const reportBox = document.getElementById("reportContainer");
  const reportBody = document.getElementById("reportMarkdownBody");
  const modeTag = document.getElementById("reportModeTag");

  btn.disabled = true;
  loader.style.display = "flex";
  reportBox.style.display = "none";

  try {
    const res = await fetch("/explain", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: selectedMode })
    });

    const data = await res.json();
    loader.style.display = "none";
    btn.disabled = false;

    if (!res.ok) {
      console.error("[ERROR] 해석 요청 실패:", data.error);
      alert(data.error || "보고서 생성 중 오류가 발생했습니다.");
      return;
    }

    console.log("[EXPLAIN] 응답 수신 완료");
    latestReportMarkdown = data.report_markdown || "";

    // 모드 태그 설정
    if (selectedMode === "B") {
      modeTag.className = "report-mode-tag";
      modeTag.style.backgroundColor = "#d1fae5";
      modeTag.style.color = "#065f46";
      modeTag.textContent = "모드 B: Pandas 계산값 기반 정식 보고서";
    } else {
      modeTag.className = "report-mode-tag";
      modeTag.style.backgroundColor = "#fee2e2";
      modeTag.style.color = "#991b1b";
      modeTag.textContent = "모드 A: 계산값 없는 비교용 모의 보고서";
    }

    // 마크다운 파싱 렌더링
    if (window.marked) {
      reportBody.innerHTML = marked.parse(latestReportMarkdown);
    } else {
      reportBody.textContent = latestReportMarkdown;
    }

    reportBox.style.display = "block";
    reportBox.scrollIntoView({ behavior: "smooth" });

    // 후속 질문 대화 영역 노출 및 대화 기록 초기화
    const followupSec = document.getElementById("followupSection");
    if (followupSec) {
      followupSec.style.display = "block";
      document.getElementById("chatHistory").innerHTML = "";
    }

  } catch (err) {
    loader.style.display = "none";
    btn.disabled = false;
    console.error("[ERROR] 해석 통신 오류:", err);
    alert("보고서 생성 중 통신 오류가 발생했습니다.");
  }
}

// ==============================================================================
// 7. 후속 질문 전송 및 대화 말풍선 렌더링 (/followup)
// ==============================================================================
async function sendFollowupQuestion(question) {
  if (!question) return;

  const chatHistory = document.getElementById("chatHistory");
  const input = document.getElementById("followupInput");
  const loader = document.getElementById("followupLoader");
  const btn = document.getElementById("btnSendFollowup");

  // 1. 사용자 질문 말풍선 생성
  const userBubble = document.createElement("div");
  userBubble.className = "chat-bubble chat-user";
  userBubble.innerHTML = `
    <div class="chat-bubble-title">🙋 나의 질문</div>
    <div>${escapeHtml(question)}</div>
  `;
  chatHistory.appendChild(userBubble);

  input.value = "";
  btn.disabled = true;
  loader.style.display = "flex";
  userBubble.scrollIntoView({ behavior: "smooth" });

  console.log(`[FOLLOWUP] 후속 질문 요청: "${question}"`);

  try {
    const res = await fetch("/followup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: question })
    });

    const data = await res.json();
    loader.style.display = "none";
    btn.disabled = false;

    if (!res.ok) {
      console.error("[ERROR] 후속 질문 처리 실패:", data.error);
      const errBubble = document.createElement("div");
      errBubble.className = "chat-bubble chat-ai";
      errBubble.style.borderLeftColor = "var(--danger)";
      errBubble.innerHTML = `
        <div class="chat-bubble-title" style="color: var(--danger);">⚠️ 안내</div>
        <div>${escapeHtml(data.error || "답변을 가져오는 중 오류가 발생했습니다.")}</div>
      `;
      chatHistory.appendChild(errBubble);
      return;
    }

    console.log(`[FOLLOWUP] 응답 수신 완료 (모델: ${data.model_used})`);

    // 2. AI 답변 말풍선 생성
    const aiBubble = document.createElement("div");
    aiBubble.className = "chat-bubble chat-ai";
    const answerHtml = window.marked ? marked.parse(data.answer_markdown) : escapeHtml(data.answer_markdown);
    aiBubble.innerHTML = `
      <div class="chat-bubble-title">🤖 AI 분석 코치 답변 (${data.model_used || "Gemini"})</div>
      <div class="markdown-body">${answerHtml}</div>
    `;
    chatHistory.appendChild(aiBubble);
    aiBubble.scrollIntoView({ behavior: "smooth" });

  } catch (err) {
    loader.style.display = "none";
    btn.disabled = false;
    console.error("[ERROR] 후속 질문 통신 오류:", err);
    const errBubble = document.createElement("div");
    errBubble.className = "chat-bubble chat-ai";
    errBubble.style.borderLeftColor = "var(--danger)";
    errBubble.innerHTML = `
      <div class="chat-bubble-title" style="color: var(--danger);">⚠️ 통신 오류</div>
      <div>서버와 통신하는 중 문제가 발생했습니다. 잠시 후 다시 시도해 주세요.</div>
    `;
    chatHistory.appendChild(errBubble);
  }
}

function escapeHtml(str) {
  if (!str) return "";
  return str.replace(/[&<>"']/g, function(m) {
    return {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    }[m];
  });
}

// ==============================================================================
// 8. 💡 2.0 데이터 자연어 질문 처리 및 시각화 매핑 (/query_ai)
// ==============================================================================
async function handleNlQuery(query) {
  if (!query) return;

  const loader = document.getElementById("nlQueryLoader");
  const resultCard = document.getElementById("nlQueryResultCard");
  const answerText = document.getElementById("nlAnswerText");
  const toolBadge = document.getElementById("nlToolBadge");
  const vizBtn = document.getElementById("btnNlExecuteViz");
  const btn = document.getElementById("btnSendNlQuery");

  btn.disabled = true;
  loader.style.display = "flex";
  resultCard.style.display = "none";
  loader.scrollIntoView({ behavior: "smooth" });

  console.log(`[QUERY_AI] 자연어 질문 분석 요청: "${query}"`);

  try {
    const res = await fetch("/query_ai", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: query })
    });

    const data = await res.json();
    loader.style.display = "none";
    btn.disabled = false;

    if (!res.ok) {
      console.error("[ERROR] 자연어 질문 분석 실패:", data.error);
      alert(data.error || "질문 분석 중 오류가 발생했습니다.");
      return;
    }

    console.log("[QUERY_AI] 응답 수신 성공:", data);
    latestNlRecommendation = data.recommended_tool;

    // 1. 자연어 답변 마크다운 렌더링
    if (window.marked) {
      answerText.innerHTML = marked.parse(data.answer);
    } else {
      answerText.textContent = data.answer;
    }

    // 2. 매핑된 도구 뱃지 및 시각화 버튼 텍스트 설정
    const toolInfo = data.recommended_tool || {};
    toolBadge.textContent = `🎯 매핑 도구: ${toolInfo.tool_label || toolInfo.tool}`;
    vizBtn.textContent = `🔄 ${toolInfo.button_text || '시각화 그래프 다시 계산하기'}`;

    resultCard.style.display = "block";

    // 🚀 질문 바로 아래 인라인 영역에 시각화 그래프 즉시 자동 생성!
    if (toolInfo.tool && toolInfo.params) {
      executeNlInlineViz(toolInfo.tool, toolInfo.params, toolInfo.button_text);
    } else {
      resultCard.scrollIntoView({ behavior: "smooth" });
    }

  } catch (err) {
    loader.style.display = "none";
    btn.disabled = false;
    console.error("[ERROR] 자연어 질문 통신 오류:", err);
    alert("서버와 통신하는 중 문제가 발생했습니다. 잠시 후 다시 시도해 주세요.");
  }
}

// ==============================================================================
// 9. 앱 접속 비밀번호 인증 모달 관리 로직
// ==============================================================================
async function initAuth() {
  setupAuthEventListeners();
  try {
    const res = await fetch("/api/auth-status");
    const data = await res.json();
    if (data.password_required) {
      if (data.authenticated) {
        hideAuthOverlay();
      } else {
        showAuthOverlay();
      }
    } else {
      hideAuthOverlay();
    }
  } catch (err) {
    console.error("[AUTH] 인증 상태 확인 실패:", err);
    showAuthOverlay();
  }
}

function showAuthOverlay(msg = "") {
  const overlay = document.getElementById("authOverlay");
  const input = document.getElementById("appPasswordInput");
  const errorBox = document.getElementById("authErrorMsg");
  const btnLogout = document.getElementById("btnLogout");

  if (overlay) overlay.style.display = "flex";
  if (btnLogout) btnLogout.style.display = "none";
  if (input) {
    input.value = "";
    setTimeout(() => input.focus(), 150);
  }
  if (errorBox) {
    if (msg) {
      errorBox.textContent = msg;
      errorBox.style.display = "block";
    } else {
      errorBox.style.display = "none";
    }
  }
}

function hideAuthOverlay() {
  const overlay = document.getElementById("authOverlay");
  const btnLogout = document.getElementById("btnLogout");
  const errorBox = document.getElementById("authErrorMsg");

  if (overlay) overlay.style.display = "none";
  if (btnLogout) btnLogout.style.display = "inline-flex";
  if (errorBox) errorBox.style.display = "none";
}

function setupAuthEventListeners() {
  const input = document.getElementById("appPasswordInput");
  const btnSubmit = document.getElementById("btnLoginSubmit");
  const btnToggle = document.getElementById("btnToggleAuthPassword");
  const btnLogout = document.getElementById("btnLogout");

  if (btnSubmit) {
    btnSubmit.addEventListener("click", handleLogin);
  }

  if (input) {
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        handleLogin();
      }
    });
  }

  if (btnToggle && input) {
    btnToggle.addEventListener("click", () => {
      if (input.type === "password") {
        input.type = "text";
        btnToggle.textContent = "🙈";
      } else {
        input.type = "password";
        btnToggle.textContent = "👁️";
      }
    });
  }

  if (btnLogout) {
    btnLogout.addEventListener("click", handleLogout);
  }
}

async function handleLogin() {
  const input = document.getElementById("appPasswordInput");
  const errorBox = document.getElementById("authErrorMsg");
  const btnSubmit = document.getElementById("btnLoginSubmit");
  const pwd = input ? input.value.trim() : "";

  if (!pwd) {
    if (errorBox) {
      errorBox.textContent = "접속 비밀번호를 입력해 주세요.";
      errorBox.style.display = "block";
      errorBox.classList.remove("shake-anim");
      void errorBox.offsetWidth;
      errorBox.classList.add("shake-anim");
    }
    input?.focus();
    return;
  }

  if (btnSubmit) {
    btnSubmit.disabled = true;
    btnSubmit.textContent = "인증 확인 중...";
  }

  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pwd })
    });
    const data = await res.json();

    if (res.ok && data.success) {
      hideAuthOverlay();
      checkServerHealth();
    } else {
      if (errorBox) {
        errorBox.textContent = data.error || "비밀번호가 일치하지 않습니다. 다시 확인해 주세요.";
        errorBox.style.display = "block";
        errorBox.classList.remove("shake-anim");
        void errorBox.offsetWidth;
        errorBox.classList.add("shake-anim");
      }
      if (input) {
        input.select();
      }
    }
  } catch (err) {
    console.error("[AUTH] 로그인 요청 오류:", err);
    if (errorBox) {
      errorBox.textContent = "서버 통신 중 오류가 발생했습니다.";
      errorBox.style.display = "block";
    }
  } finally {
    if (btnSubmit) {
      btnSubmit.disabled = false;
      btnSubmit.textContent = "🚀 앱 접속하기";
    }
  }
}

async function handleLogout() {
  try {
    await fetch("/api/logout", { method: "POST" });
  } catch (err) {
    console.warn("[AUTH] 로그아웃 요청 실패 (무시):", err);
  }
  showAuthOverlay("로그아웃되었습니다. 다시 접속하려면 비밀번호를 입력해 주세요.");
}

// ==============================================================================
// 10. PWA (Progressive Web App) 서비스 워커 등록 및 설치 프롬프트 제어
// ==============================================================================
let deferredInstallPrompt = null;

function registerServiceWorker() {
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker
        .register("/sw.js")
        .then((reg) => {
          console.log("[PWA] Service Worker 등록 완료. 스코프:", reg.scope);
        })
        .catch((err) => {
          console.warn("[PWA] Service Worker 등록 생략 또는 실패:", err);
        });
    });
  }
}

function setupPwaInstall() {
  const btnInstall = document.getElementById("btnInstallPwa");
  const iosModal = document.getElementById("iosInstallModal");
  const btnCloseIos = document.getElementById("btnCloseIosModal");
  const btnConfirmIos = document.getElementById("btnConfirmIosModal");

  // iOS Safari 환경 감지
  const isIos = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
  const isStandalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone;

  // 이미 PWA 단독 앱(Standalone) 모드로 실행 중이면 설치 버튼 숨김
  if (isStandalone) {
    console.log("[PWA] 이미 독립형 앱으로 실행 중입니다.");
    if (btnInstall) btnInstall.style.display = "none";
    return;
  }

  // 모바일/데스크톱 브라우저의 PWA 설치 이벤트 캐치
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    deferredInstallPrompt = e;
    console.log("[PWA] beforeinstallprompt 준비 완료");
    if (btnInstall) {
      btnInstall.style.display = "inline-flex";
    }
  });

  // iOS Safari의 경우 홈 화면 추가 안내 버튼 상시 활성화
  if (isIos && !isStandalone) {
    if (btnInstall) {
      btnInstall.style.display = "inline-flex";
      btnInstall.textContent = "📲 홈 화면 추가";
    }
  }

  // PWA 설치 완료 시
  window.addEventListener("appinstalled", () => {
    console.log("[PWA] 앱 설치가 성공적으로 완료되었습니다.");
    deferredInstallPrompt = null;
    if (btnInstall) btnInstall.style.display = "none";
  });

  // 설치 버튼 클릭 처리
  if (btnInstall) {
    btnInstall.addEventListener("click", async () => {
      if (deferredInstallPrompt) {
        deferredInstallPrompt.prompt();
        const choice = await deferredInstallPrompt.userChoice;
        console.log("[PWA] 사용자 응답:", choice.outcome);
        if (choice.outcome === "accepted") {
          btnInstall.style.display = "none";
        }
        deferredInstallPrompt = null;
      } else if (isIos) {
        if (iosModal) iosModal.style.display = "flex";
      } else {
        // 브라우저 자체 설치 기능 유도
        alert("브라우저 우측 상단의 '설치' 아이콘(또는 브라우저 메뉴의 '홈 화면에 추가'/'앱으로 설치')을 눌러 설치할 수 있습니다!");
      }
    });
  }

  // iOS 안내 모달 닫기
  if (btnCloseIos && iosModal) {
    btnCloseIos.addEventListener("click", () => {
      iosModal.style.display = "none";
    });
  }
  if (btnConfirmIos && iosModal) {
    btnConfirmIos.addEventListener("click", () => {
      iosModal.style.display = "none";
    });
  }
}
