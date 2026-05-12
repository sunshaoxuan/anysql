/**
 * AnySQL — 前端逻辑
 */

// --- 健康检查 ---
async function checkHealth() {
    try {
        const resp = await fetch('/api/health');
        const data = await resp.json();
        const dot = document.querySelector('.status-dot');
        const text = document.querySelector('.status-text');
        if (data.status === 'ok' && data.llm && data.llm.status === 'ok') {
            dot.style.background = 'var(--success)';
            text.textContent = 'LLM接続済';
        } else {
            dot.style.background = 'var(--warning)';
            text.textContent = 'LLM異常';
        }
    } catch {
        const dot = document.querySelector('.status-dot');
        const text = document.querySelector('.status-text');
        if (dot) { dot.style.background = 'var(--error)'; }
        if (text) { text.textContent = 'オフライン'; }
    }
}

// --- 搜索 ---
async function performSearch() {
    const query = document.getElementById('search-input').value.trim();
    if (!query) return;

    const product = document.getElementById('product-filter').value;
    const btn = document.getElementById('search-btn');
    const resultsSection = document.getElementById('results-section');
    const emptyState = document.getElementById('empty-state');

    btn.textContent = '検索中...';
    btn.disabled = true;

    try {
        const resp = await fetch('/api/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                query: query,
                product: product || null,
                top_k: 15
            })
        });
        const data = await resp.json();

        if (data.results && data.results.length > 0) {
            renderResults(data);
            resultsSection.style.display = 'block';
            if (emptyState) emptyState.style.display = 'none';
        } else {
            resultsSection.style.display = 'block';
            document.getElementById('results-list').innerHTML =
                '<div class="empty-message">一致するSQLが見つかりませんでした</div>';
            document.getElementById('results-count').textContent = '0件';
            document.getElementById('results-time').textContent = `${data.elapsed_ms}ms`;
            if (emptyState) emptyState.style.display = 'none';
        }
    } catch (err) {
        console.error('Search error:', err);
        resultsSection.style.display = 'block';
        document.getElementById('results-list').innerHTML =
            `<div class="error-message">検索エラー: ${err.message}</div>`;
    } finally {
        btn.textContent = '検索';
        btn.disabled = false;
    }
}

function renderResults(data) {
    document.getElementById('results-count').textContent = `${data.total}件の結果`;
    document.getElementById('results-time').textContent = `${data.elapsed_ms}ms`;

    const list = document.getElementById('results-list');
    list.innerHTML = '';

    data.results.forEach(r => {
        const scorePercent = Math.round(r.score * 100);
        const tags = [
            ...(r.category || []).map(c => `<span class="tag tag-category">${c}</span>`),
            ...(r.keywords || []).slice(0, 3).map(k => `<span class="tag tag-keyword">${k}</span>`)
        ].join('');

        const card = document.createElement('div');
        card.className = 'result-card';
        card.onclick = () => location.href = `/sql/${r.sql_id}`;
        card.innerHTML = `
            <span class="result-score">${scorePercent}%</span>
            <div class="result-summary">${r.summary || r.comment || r.sql_id}</div>
            <div class="result-sql">${escapeHtml(r.raw_sql)}</div>
            <div class="result-meta">
                <span>📁 ${r.source_file}</span>
                <span>📦 ${r.product}</span>
            </div>
            ${tags ? `<div class="result-tags">${tags}</div>` : ''}
        `;
        list.appendChild(card);
    });
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// --- Init ---
document.addEventListener('DOMContentLoaded', () => {
    checkHealth();
    setInterval(checkHealth, 30000);
});
