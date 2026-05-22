// app.js

const MAX_HISTORY = 6;
let history = []; // [{role, content}]
let sessionTotals = { input: 0, output: 0 };

// DOM Refs
const apiUrlInput = document.getElementById("apiUrlInput");
const pipelineRadios = document.getElementsByName("pipelineMode");
const exampleQueriesDiv = document.getElementById("exampleQueries");
const clearChatBtn = document.getElementById("clearChatBtn");
const healthBadge = document.getElementById("healthBadge");
const messagesInner = document.getElementById("messagesInner");
const userInput = document.getElementById("userInput");
const sendBtn = document.getElementById("sendBtn");

// Panel Refs
const totIn = document.getElementById("totIn");
const totOut = document.getElementById("totOut");
const totTok = document.getElementById("totTok");
const totCost = document.getElementById("totCost");
const lastRequestPanel = document.getElementById("lastRequestPanel");
const lastIn = document.getElementById("lastIn");
const lastOut = document.getElementById("lastOut");
const lastCost = document.getElementById("lastCost");
const intentPanel = document.getElementById("intentPanel");
const sqlPanel = document.getElementById("sqlPanel");

const INPUT_COST_PER_M = 1.50;
const OUTPUT_COST_PER_M = 9.00;

// Init
const examples = [
    "What is the current flow rate?",
    "Show energy consumed by pump 2 today",
    "How many times did pump 1 start last week?",
    "Compare SEC of all pumps for May 2026",
    "What is the wet well level now?",
    "Show runtime of all pumps yesterday",
    "Power factor of pump 3 last 7 days",
    "How many pumps are running right now?"
];

examples.forEach(ex => {
    const btn = document.createElement("button");
    btn.className = "action-btn w-full";
    btn.style.textAlign = "left";
    btn.textContent = ex;
    btn.onclick = () => {
        userInput.value = ex;
        sendMessage();
    };
    exampleQueriesDiv.appendChild(btn);
});

clearChatBtn.onclick = () => {
    history = [];
    messagesInner.innerHTML = "";
    sessionTotals = { input: 0, output: 0 };
    updateTokenUI();
    intentPanel.innerHTML = `<div class="empty-state">Ask a question to see intent analysis here</div>`;
    sqlPanel.innerHTML = `<div class="empty-state">SQL will appear here after a successful query</div>`;
};

// Health Check
async function checkHealth() {
    try {
        const url = apiUrlInput.value.replace(/\/$/, "");
        const res = await fetch(`${url}/health`, { signal: AbortSignal.timeout(2000) });
        if (res.ok) {
            healthBadge.className = "pill-on";
            healthBadge.textContent = "● API Online";
        } else {
            healthBadge.className = "pill-warn";
            healthBadge.textContent = "● API Error";
        }
    } catch {
        healthBadge.className = "pill-off";
        healthBadge.textContent = "● API Offline";
    }
}
setInterval(checkHealth, 10000);
checkHealth();
apiUrlInput.onchange = checkHealth;

// Send Message
userInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendMessage();
});
sendBtn.onclick = sendMessage;

function getPipelineMode() {
    for (let r of pipelineRadios) {
        if (r.checked) return r.value;
    }
    return "full";
}

async function sendMessage() {
    const text = userInput.value.trim();
    if (!text) return;

    appendUserBubble(text);
    userInput.value = "";

    const mode = getPipelineMode();
    const url = apiUrlInput.value.replace(/\/$/, "");
    const endpoint = mode === "intent_only" ? "/parse-intent" : "/query";

    // Show loading
    const loaderId = appendThinkingBubble();

    try {
        const payload = {
            user_message: text,
            conversation_history: history.slice(-MAX_HISTORY)
        };
        const res = await fetch(`${url}${endpoint}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "API Error");

        // Update Token Usage
        let usage = null;
        if (mode === "intent_only") {
            usage = data.raw_json?._usage || {};
            // adapt structure
            usage = {
                prompt_tokens: usage.promptTokenCount || 0,
                candidates_tokens: usage.candidatesTokenCount || 0
            };
        } else {
            usage = data.token_usage || { prompt_tokens: 0, candidates_tokens: 0 };
        }
        updateTokens(usage.prompt_tokens, usage.candidates_tokens);

        // Update Panels and Chat
        if (mode === "intent_only") {
            updateBotBubble(loaderId, "✅ Intent parsed successfully. See the panel →", data.intent);
            renderIntentPanel(data);
            sqlPanel.innerHTML = `<div class="empty-state">SQL generation is disabled in Intent Only mode.</div>`;
        } else {
            if (data.clarification_needed) {
                updateBotBubble(loaderId, data.clarification_message, null, true, data.clarification_options || [], text);
                renderIntentPanel(data.intent_json);
            } else if (!data.sql_query && data.clarification_message) {
                // out_of_scope / greeting / unknown — no SQL, but has a message
                updateBotBubble(loaderId, data.clarification_message, data.intent_json?.intent);
                renderIntentPanel(data.intent_json);
                renderSqlPanel(data);
            } else {
                updateBotBubble(loaderId, data.final_answer || "✅ SQL query generated.", data.intent_json?.intent);
                renderIntentPanel(data.intent_json);
                renderSqlPanel(data);
            }
        }

        history.push({ role: "user", content: text });
        history.push({ role: "assistant", content: data.final_answer || data.clarification_message || "✅ Intent parsed." });

    } catch (e) {
        updateBotBubble(loaderId, `❌ Error: ${e.message}`, "error");
    }
}

function appendUserBubble(text) {
    const div = document.createElement("div");
    div.className = "user-bubble";

    const timeStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const timeDiv = document.createElement("div");
    timeDiv.style.fontSize = "0.75rem";
    timeDiv.style.opacity = "0.7";
    timeDiv.style.marginBottom = "4px";
    timeDiv.textContent = timeStr;

    const textDiv = document.createElement("div");
    textDiv.textContent = text;

    div.appendChild(timeDiv);
    div.appendChild(textDiv);

    messagesInner.appendChild(div);
    scrollToBottom();
}

// Rotating phrases shown while waiting for the API
const THINKING_PHRASES = [
    "Pondering your query…",
    "Crunching the numbers…",
    "Consulting the database…",
    "Brewing an answer…",
    "Thinking hard…",
    "Parsing your intent…",
    "Almost there…",
    "Running the SQL…",
    "Connecting the dots…",
    "Analysing data…",
];

function appendThinkingBubble() {
    const id = "msg-" + Date.now();
    const div = document.createElement("div");
    div.id = id;
    div.className = "thinking-bubble";
    div.innerHTML = `
        <div class="thinking-dots">
            <span></span><span></span><span></span>
        </div>
        <span class="thinking-label" id="${id}-label">${THINKING_PHRASES[0]}</span>
    `;
    messagesInner.appendChild(div);
    scrollToBottom();

    // Cycle through phrases every 1.8 s
    let idx = 1;
    const label = div.querySelector(`#${id}-label`);
    const interval = setInterval(() => {
        if (!document.getElementById(id)) { clearInterval(interval); return; }
        label.style.opacity = "0";
        setTimeout(() => {
            label.textContent = THINKING_PHRASES[idx % THINKING_PHRASES.length];
            label.style.opacity = "1";
            idx++;
        }, 400);
    }, 1800);
    // Store interval so updateBotBubble can clear it
    div._thinkingInterval = interval;
    return id;
}

function appendBotBubble(content, intent = null) {
    const id = "msg-" + Date.now();
    const div = document.createElement("div");
    div.id = id;
    div.className = "bot-bubble";
    div.innerHTML = formatBotContent(content, intent);
    messagesInner.appendChild(div);
    scrollToBottom();
    return id;
}

function updateBotBubble(id, content, intent = null, isClarify = false, clarifyOptions = [], originalQuery = "") {
    const div = document.getElementById(id);
    if (div) {
        // Clear the thinking phrase interval if present
        if (div._thinkingInterval) clearInterval(div._thinkingInterval);
        div.className = isClarify ? "clarify-bubble" : "bot-bubble";
        if (isClarify) {
            let html = `⚠️ <b>Clarification needed:</b><br>${content}`;

            if (clarifyOptions && clarifyOptions.length > 0) {
                const iconMap = {
                    'today': '📅',
                    'yesterday': '⏮️',
                    'last 7 days': '📆',
                    'last 30 days': '🗓️',
                    'this month': '📋',
                    'last hour': '⏱️',
                    'last 24 hours': '🕐',
                };
                html += `<div class="clarify-options"><span class="clarify-options-label">Quick select</span><div class="clarify-chips-row">`;
                clarifyOptions.forEach(opt => {
                    const escapedOpt = opt.replace(/'/g, "\\'");
                    const escapedQuery = originalQuery.replace(/'/g, "\\'");
                    const icon = iconMap[opt] || '🕒';
                    html += `<button class="clarify-option-btn" data-opt="${opt}" onclick="handleClarifyOption('${escapedQuery}', '${escapedOpt}')">${icon} ${opt}</button>`;
                });
                html += `</div></div>`;
            }

            div.innerHTML = html;
        } else {
            div.innerHTML = formatBotContent(content, intent);
        }
    }
    scrollToBottom();
}

function handleClarifyOption(originalQuery, option) {
    // Build the refined query by appending the chosen time-range option
    const refinedQuery = `${originalQuery} ${option}`;
    userInput.value = refinedQuery;
    sendMessage();
}

function formatBotContent(text, intent) {
    let res = "";
    if (intent && intent !== "error") {
        res += `<div class="intent-badge">🎯 ${intent}</div>`;
    }
    res += `<div>${renderMarkdown(text)}</div>`;
    return res;
}

function scrollToBottom() {
    const wrap = document.getElementById("messagesWrap");
    wrap.scrollTop = wrap.scrollHeight;
}

// Render Panels
function renderIntentPanel(intent) {
    if (!intent) return;

    let pumpsStr = "All";
    if (intent.pumps && intent.pumps.length > 0) pumpsStr = intent.pumps.join(", ");

    let html = `
        <div class="metrics-row">
            <div class="metric-card"><div class="metric-val">${(intent.intent || "—").replace(/_/g, " ").slice(0, 10)}</div><div class="metric-lbl">Intent</div></div>
            <div class="metric-card"><div class="metric-val">${pumpsStr}</div><div class="metric-lbl">Pumps</div></div>
            <div class="metric-card"><div class="metric-val">${(intent.aggregation || "—").toUpperCase()}</div><div class="metric-lbl">Agg</div></div>
        </div>
        <div class="intent-flags">
    `;

    if (intent.requires_lag) html += `<span class="pill-warn">LAG Req</span>`;
    if (intent.clarification_needed) html += `<span class="pill-off">Needs Clarify</span>`;
    if (intent.limit) html += `<span class="pill-on">LIMIT ${intent.limit}</span>`;
    if (intent.group_by) html += `<span class="pill-on">GROUP BY</span>`;

    html += `</div><div class="intent-details">`;

    const tr = intent.time_range || {};
    if (tr.type !== "none") {
        html += `<div>🕐 <b>Time Range:</b> ${tr.relative || (tr.start + " → " + tr.end)}</div>`;
    }
    if (intent.metrics && intent.metrics.length > 0) {
        html += `<div>📊 <b>Metrics:</b> ${intent.metrics.join(", ")}</div>`;
    }

    html += `</div>
        <div class="json-expander" onclick="this.classList.toggle('open')">
            <div class="json-expander-header">📋 Full Intent JSON <span>▼</span></div>
            <pre class="json-block">${JSON.stringify(intent, null, 2)}</pre>
        </div>
    `;
    intentPanel.innerHTML = html;
}

function renderSqlPanel(data) {
    if (!data.sql_query) {
        sqlPanel.innerHTML = `<div class="empty-state">No SQL generated for this query.</div>`;
        return;
    }

    let html = "";
    if (data.db_error) {
        html += `<div class="error-banner">⚠️ DB Error: ${data.db_error}</div>`;
    }

    html += `<div class="sql-block">${data.sql_query}</div>`;

    const lines = data.sql_query.split("\\n").length;
    const rows = data.db_results ? data.db_results.length : 0;

    html += `
        <div class="sql-stats">
            <div class="sql-stat-btn sql-copy-btn" onclick="navigator.clipboard.writeText(\`${data.sql_query.replace(/`/g, '\\`')}\`)">📋 Copy</div>
            <div class="sql-stat-btn">${lines} lines</div>
            <div class="sql-stat-btn" style="color: ${rows > 0 ? 'var(--accent-green)' : 'inherit'}">🗃️ ${rows} rows</div>
        </div>
    `;
    sqlPanel.innerHTML = html;
}

function updateTokens(p_in, p_out) {
    sessionTotals.input += p_in;
    sessionTotals.output += p_out;

    const lastC = (p_in / 1000000 * INPUT_COST_PER_M) + (p_out / 1000000 * OUTPUT_COST_PER_M);
    const totC = (sessionTotals.input / 1000000 * INPUT_COST_PER_M) + (sessionTotals.output / 1000000 * OUTPUT_COST_PER_M);

    totIn.textContent = sessionTotals.input.toLocaleString();
    totOut.textContent = sessionTotals.output.toLocaleString();
    totTok.textContent = (sessionTotals.input + sessionTotals.output).toLocaleString();
    totCost.textContent = "$" + totC.toFixed(6);

    if (p_in > 0 || p_out > 0) {
        lastRequestPanel.style.display = "block";
        lastIn.textContent = p_in.toLocaleString();
        lastOut.textContent = p_out.toLocaleString();
        lastCost.textContent = "$" + lastC.toFixed(6);
    }
}

function updateTokenUI() {
    totIn.textContent = "0";
    totOut.textContent = "0";
    totTok.textContent = "0";
    totCost.textContent = "$0.000000";
    lastRequestPanel.style.display = "none";
}

// Simple Markdown Render — with LaTeX-style table support
function renderMarkdown(text) {
    if (!text) return "";

    // Split into lines for table detection
    const lines = text.split('\n');
    const outputParts = [];
    let i = 0;

    while (i < lines.length) {
        // Detect a markdown table: a pipe row, followed by a separator row (---|---)
        const isTableRow = (line) => line.trim().startsWith('|') && line.trim().endsWith('|');
        const isSepRow = (line) => /^\s*\|?(\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$/.test(line);

        if (isTableRow(lines[i]) && i + 1 < lines.length && (isSepRow(lines[i + 1]) || isTableRow(lines[i + 1]))) {
            // Collect all contiguous table lines
            const tableLines = [];
            while (i < lines.length && (isTableRow(lines[i]) || isSepRow(lines[i]))) {
                tableLines.push(lines[i]);
                i++;
            }

            // Parse header (first row) and body (remaining non-sep rows)
            const parseRow = (line) =>
                line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());

            const headerCells = parseRow(tableLines[0]);
            const bodyRows = tableLines
                .slice(1)
                .filter(l => !isSepRow(l))
                .map(parseRow);

            let tableHtml = '<div class="latex-table-wrap"><table class="latex-table"><thead><tr>';
            headerCells.forEach(cell => {
                tableHtml += `<th>${inlineMarkdown(cell)}</th>`;
            });
            tableHtml += '</tr></thead><tbody>';
            bodyRows.forEach(row => {
                tableHtml += '<tr>';
                row.forEach(cell => {
                    tableHtml += `<td>${inlineMarkdown(cell)}</td>`;
                });
                tableHtml += '</tr>';
            });
            tableHtml += '</tbody></table></div>';
            outputParts.push(tableHtml);
        } else {
            // Regular line: apply inline markdown then convert newline
            outputParts.push(inlineMarkdown(lines[i]) + (i < lines.length - 1 ? '<br/>' : ''));
            i++;
        }
    }

    return outputParts.join('');
}

// Inline-only markdown (bold, italic, code) — no newline handling
function inlineMarkdown(text) {
    if (!text) return '';
    return text
        .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
        .replace(/\*(.*?)\*/g, '<em>$1</em>')
        .replace(/`(.*?)`/g, '<code style="background:var(--bg-300);padding:2px 4px;border-radius:4px;color:var(--accent-blue);font-family:var(--font-mono);font-size:12px;">$1</code>');
}