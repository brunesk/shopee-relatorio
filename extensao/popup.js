const API = 'https://shopee-relatorio-production.up.railway.app';
const root = document.getElementById('root');

function fmt(val) {
  return 'R$ ' + Number(val).toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function showLoading(msg) {
  root.innerHTML = `<div class="loading"><div class="spinner"></div>${msg}</div>`;
}

function showError(msg) {
  root.innerHTML = `<div class="error">⚠️ ${msg}</div>`;
}

function showWarning(msg) {
  root.innerHTML = `<div class="warning">${msg}</div>`;
}

function renderResult(d) {
  const produtos = (d.produtos || []).slice(0, 20);
  const insights = d.insights || [];

  root.innerHTML = `
    <div class="kpis">
      <div class="kpi">
        <div class="kpi-label">Faturamento 30d</div>
        <div class="kpi-value green">${fmt(d.faturamento_30d)}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Vendas 30d</div>
        <div class="kpi-value orange">${d.total_vendas_30d}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Produtos</div>
        <div class="kpi-value">${d.total_produtos}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Loja</div>
        <div class="kpi-value" style="font-size:13px">${d.loja}</div>
      </div>
    </div>

    <div class="section-title">Produtos — preços de compra para lucro líquido</div>
    <div class="product-list">
      ${produtos.map((p, i) => `
        <div class="product-item" style="flex-direction:column;gap:6px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div class="product-name" style="font-weight:600">${i + 1}. ${p.nome}</div>
            <div style="text-align:right;flex-shrink:0">
              <div class="product-price">${fmt(p.preco)}</div>
              <div class="product-sold">▲ ${p.vendas_30d} vendas/mês</div>
            </div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;font-size:11px">
            <div style="background:#0f2d1a;border-radius:4px;padding:5px 7px">
              <div style="color:#4ade80;font-weight:700">20% lucro</div>
              <div style="color:#f1f5f9">até ${fmt(p.custo_max_20pct)}</div>
            </div>
            <div style="background:#0f2320;border-radius:4px;padding:5px 7px">
              <div style="color:#34d399;font-weight:700">25% lucro</div>
              <div style="color:#f1f5f9">até ${fmt(p.custo_max_25pct)}</div>
            </div>
            <div style="background:#0a1f2a;border-radius:4px;padding:5px 7px">
              <div style="color:#60a5fa;font-weight:700">30% lucro</div>
              <div style="color:#f1f5f9">${p.custo_max_30pct > 0 ? 'até ' + fmt(p.custo_max_30pct) : '⚠️ inviável'}</div>
            </div>
          </div>
          <div style="font-size:10px;color:#475569">
            Você recebe ${fmt(p.recebido_apos_taxas)} após todas as taxas da Shopee • Fat. ${fmt(p.faturamento_30d)}/mês
          </div>
        </div>
      `).join('')}
    </div>

    ${insights.length ? `
      <div class="section-title">Insights</div>
      <div class="insights">
        ${insights.map(ins => `
          <div class="insight ${ins.tipo}">
            <div class="insight-title">${ins.titulo}</div>
            <div class="insight-desc">${ins.descricao}</div>
          </div>
        `).join('')}
      </div>
    ` : ''}
  `;
}

// Função que roda DENTRO do browser do usuário (contexto real da Shopee)
async function coletarDadosShopee(shopUsername) {
  try {
    // Busca shop_id
    const shopResp = await fetch(
      `https://shopee.com.br/api/v4/shop/get_shop_detail?username=${shopUsername}`,
      { credentials: 'include' }
    );
    const shopJson = await shopResp.json();
    const shopId = shopJson?.data?.shopid;
    const shopName = shopJson?.data?.name;
    if (!shopId) return { error: 'Loja não encontrada' };

    // Busca produtos (com cookies e tokens reais do usuário)
    const prodResp = await fetch(
      `https://shopee.com.br/api/v4/search/search_items?by=sales&match_id=${shopId}&order=desc&page_type=shop&scenario=PAGE_OTHERS&version=2&limit=100&offset=0`,
      {
        credentials: 'include',
        headers: { 'x-api-source': 'pc', 'x-shopee-language': 'pt-BR' }
      }
    );
    const prodJson = await prodResp.json();

    if (prodJson?.error && prodJson.error !== 0) {
      return { error: `API retornou erro ${prodJson.error}`, raw: prodJson };
    }

    return { shopId: String(shopId), shopName, produtos: prodJson };
  } catch (e) {
    return { error: String(e) };
  }
}

async function main() {
  // Verifica aba atual
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

  if (!tab.url || !tab.url.includes('shopee.com.br')) {
    showWarning('Abra uma página de loja do Shopee e clique novamente.<br><br>Exemplo: <b>shopee.com.br/nomedadaloja</b>');
    return;
  }

  const urlObj = new URL(tab.url);
  const username = urlObj.pathname.split('/').filter(Boolean)[0];

  if (!username || username.length < 2) {
    showWarning('Navegue até a página de uma loja específica.<br><br>Exemplo: <b>shopee.com.br/nomedadaloja</b>');
    return;
  }

  // Mostra botão inicial
  root.innerHTML = `
    <div class="shop-detected">
      <strong>@${username}</strong>
      Loja detectada — clique para analisar
    </div>
    <button class="btn" id="btn-analisar">🔍 Analisar Loja</button>
  `;

  document.getElementById('btn-analisar').addEventListener('click', async () => {
    showLoading('Coletando dados da loja...');

    let dadosShopee;
    try {
      // Roda a coleta dentro do contexto real do browser (com cookies e tokens)
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        world: 'MAIN',
        func: coletarDadosShopee,
        args: [username]
      });
      dadosShopee = results[0].result;
    } catch (e) {
      showError('Não foi possível acessar a página. Recarregue a aba do Shopee e tente novamente.');
      return;
    }

    if (!dadosShopee || dadosShopee.error) {
      const msg = dadosShopee?.error || 'Erro desconhecido';
      if (dadosShopee?.raw) {
        showError(`Erro da API Shopee: ${JSON.stringify(dadosShopee.raw).substring(0, 200)}`);
      } else {
        showError(msg);
      }
      return;
    }

    showLoading('Calculando margens e insights...');

    // Envia pro backend para análise
    try {
      const resp = await fetch(`${API}/analisar-dados`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          url: `https://shopee.com.br/${username}`,
          shop_id: dadosShopee.shopId,
          shop_name: dadosShopee.shopName,
          raw_data: dadosShopee.produtos
        })
      });

      if (!resp.ok) {
        const err = await resp.json();
        showError(err.detail || 'Erro no servidor');
        return;
      }

      const analise = await resp.json();
      renderResult(analise);
    } catch (e) {
      showError('Erro ao conectar com o servidor. Tente novamente.');
    }
  });
}

main();
