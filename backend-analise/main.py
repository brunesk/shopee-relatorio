import asyncio
import re
import json
import base64
import os
import httpx
from urllib.parse import urlparse, quote
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Shopee Store Analyzer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

SCRAPINGBEE_KEY = os.environ["SCRAPINGBEE_API_KEY"]


class AnalyzeRequest(BaseModel):
    url: str


class DadosRequest(BaseModel):
    url: str
    shop_id: str
    shop_name: str
    raw_data: dict


@app.get("/")
async def root():
    return {"status": "ok", "message": "Shopee Analyzer API funcionando"}


@app.post("/analisar-dados")
async def analisar_dados(req: DadosRequest):
    """Recebe dados brutos coletados pela extensão Chrome e retorna a análise."""
    try:
        products = extract_from_json(req.raw_data, req.shop_id)
        if not products:
            raise HTTPException(status_code=400, detail="Nenhum produto encontrado. Aguarde a loja carregar e tente novamente.")

        seen = set()
        unique = []
        for p in products:
            key = p["nome"].lower()[:50]
            if key not in seen:
                seen.add(key)
                unique.append(calcular_margens(p))

        unique.sort(key=lambda x: x["faturamento_30d"], reverse=True)
        total_fat = sum(p["faturamento_30d"] for p in unique)
        total_vendas = sum(p["vendas_30d"] for p in unique)

        return {
            "loja": req.shop_name,
            "url": req.url,
            "total_produtos": len(unique),
            "faturamento_30d": round(total_fat, 2),
            "total_vendas_30d": total_vendas,
            "melhor_produto": unique[0] if unique else None,
            "produtos": unique,
            "insights": gerar_insights(unique, total_fat),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")


@app.get("/debug/{username}")
async def debug(username: str):
    """Renderiza a página com stealth_proxy e examina o HTML resultante."""
    shop_id, shop_name = await get_shop_id(username)
    shop_id_str = str(shop_id)
    sorted_url = f"https://shopee.com.br/{username}?page=0&sortBy=sales&tab=0"

    sb_url = (
        f"https://app.scrapingbee.com/api/v1/"
        f"?api_key={SCRAPINGBEE_KEY}"
        f"&url={quote(sorted_url, safe='')}"
        f"&render_js=true"
        f"&stealth_proxy=true"
        f"&country_code=br"
        f"&wait=12000"
    )

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.get(sb_url)
        html = resp.text

        # Procura por dados JSON embutidos no HTML (SSR ou inline scripts)
        inline_data = []
        for m in re.finditer(r'<script[^>]*>(\{.{100,}\})</script>', html, re.DOTALL):
            raw = m.group(1)
            try:
                data = json.loads(raw)
                products = extract_from_json(data, shop_id_str)
                if products:
                    inline_data.append({"products": len(products), "preview": str(products[:2])})
            except Exception:
                pass

        # Extrai texto visível do HTML para ver se tem nomes de produto
        # Remove tags para pegar só o texto
        text_only = re.sub(r'<[^>]+>', ' ', html)
        text_only = re.sub(r'\s+', ' ', text_only).strip()

        # Procura padrões de preço e vendas no texto
        prices = re.findall(r'R\$\s*[\d.,]+', text_only)[:10]
        sales = re.findall(r'\d+\s*vendas?', text_only, re.IGNORECASE)[:10]

        return {
            "shop_id": shop_id_str,
            "shop_name": shop_name,
            "status": resp.status_code,
            "html_size": len(html),
            "inline_scripts_with_products": inline_data,
            "prices_found_in_html": prices,
            "sales_found_in_html": sales,
            "text_preview": text_only[:3000],
        }


@app.post("/analisar")
async def analisar(req: AnalyzeRequest):
    try:
        return await scrape_and_analyze(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")


def clean_store_url(url: str):
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if not parts or not parts[0]:
        raise ValueError("Link inválido. Use: https://shopee.com.br/nomedadaloja")
    return f"https://shopee.com.br/{parts[0]}", parts[0]


async def get_shop_id(username: str) -> tuple:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"https://shopee.com.br/api/v4/shop/get_shop_detail?username={username}",
            headers={"User-Agent": UA},
        )
        data = resp.json().get("data") or {}
        shop_id = data.get("shopid") or data.get("shop_id")
        name = data.get("name") or username
        return shop_id, name


def parse_price(raw) -> float:
    if not isinstance(raw, (int, float)) or raw <= 0:
        return 0.0
    return raw / 100000 if raw > 100000 else float(raw)


def extract_from_json(obj, shop_id_str: str, depth=0) -> list:
    """Varre JSON recursivamente extraindo produtos válidos."""
    if depth > 15:
        return []
    results = []

    if isinstance(obj, list):
        for item in obj:
            results.extend(extract_from_json(item, shop_id_str, depth + 1))

    elif isinstance(obj, dict):
        core = obj.get("item_basic") if "item_basic" in obj else obj
        name = core.get("name") or core.get("item_name") or ""
        price_raw = (
            core.get("price") or core.get("price_min") or core.get("price_max") or 0
        )
        sold = core.get("sold") or core.get("historical_sold") or 0
        shopid = str(core.get("shopid") or core.get("shop_id") or "")
        price = parse_price(price_raw)

        is_product = (
            isinstance(name, str)
            and len(name) > 3
            and price > 0
            and (not shop_id_str or not shopid or shopid == shop_id_str)
        )

        if is_product:
            results.append({
                "nome": name.strip(),
                "preco": round(price, 2),
                "vendas_30d": int(sold),
                "_shopid": shopid,
            })
        else:
            for v in obj.values():
                results.extend(extract_from_json(v, shop_id_str, depth + 1))

    return results


async def fetch_via_scrapingbee_api(shop_id_str: str, username: str) -> list:
    """
    Estratégia 1: ScrapingBee como proxy para chamar o endpoint JSON da Shopee.
    IP residencial brasileiro — sem JS rendering (1 crédito).
    """
    target = (
        f"https://shopee.com.br/api/v4/search/search_items"
        f"?by=sales&match_id={shop_id_str}&order=desc"
        f"&page_type=shop&scenario=PAGE_OTHERS&version=2&limit=100&offset=0"
    )
    sb_url = (
        f"https://app.scrapingbee.com/api/v1/"
        f"?api_key={SCRAPINGBEE_KEY}"
        f"&url={quote(target, safe='')}"
        f"&render_js=false"
        f"&country_code=br"
        f"&custom_google=false"
        f"&forward_headers=true"
    )

    print("[SB-API] Tentando endpoint direto via ScrapingBee...")
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            sb_url,
            headers={
                "x-api-source": "pc",
                "x-shopee-language": "pt-BR",
                "Referer": f"https://shopee.com.br/{username}",
            },
        )
        print(f"[SB-API] HTTP {resp.status_code} | {len(resp.text)} chars")

        if resp.status_code != 200:
            print(f"[SB-API] Erro: {resp.text[:300]}")
            return []

        try:
            data = resp.json()
        except Exception:
            print(f"[SB-API] Resposta não é JSON: {resp.text[:200]}")
            return []

        products = extract_from_json(data, shop_id_str)
        print(f"[SB-API] {len(products)} produtos encontrados")
        return products


async def fetch_via_scrapingbee_render(shop_id_str: str, username: str, store_url: str) -> list:
    """
    Estratégia 2: ScrapingBee renderiza a página completa com JS.
    Injeta um snippet que chama a API de dentro do browser (tem acesso aos tokens).
    5 créditos por requisição.
    """
    sorted_url = store_url + "?page=0&sortBy=sales&tab=0"

    # JS que roda dentro da página após carregar e grava o resultado no DOM
    js_snippet = base64.b64encode(f"""
(async () => {{
    try {{
        await new Promise(r => setTimeout(r, 4000));
        const resp = await fetch(
            '/api/v4/search/search_items?by=sales&match_id={shop_id_str}&order=desc&page_type=shop&scenario=PAGE_OTHERS&version=2&limit=100&offset=0',
            {{credentials: 'include', headers: {{'x-api-source': 'pc', 'x-shopee-language': 'pt-BR'}}}}
        );
        const data = await resp.json();
        const el = document.createElement('div');
        el.id = '__scraped__';
        el.style.display = 'none';
        el.textContent = JSON.stringify(data);
        document.body.appendChild(el);
    }} catch(e) {{
        const el = document.createElement('div');
        el.id = '__scraped__';
        el.style.display = 'none';
        el.textContent = JSON.stringify({{error: String(e)}});
        document.body.appendChild(el);
    }}
}})();
""".encode()).decode()

    sb_url = (
        f"https://app.scrapingbee.com/api/v1/"
        f"?api_key={SCRAPINGBEE_KEY}"
        f"&url={quote(sorted_url, safe='')}"
        f"&render_js=true"
        f"&country_code=br"
        f"&wait=8000"
        f"&js_snippet={js_snippet}"
    )

    print("[SB-RENDER] Renderizando página com JS injetado...")
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.get(sb_url)
        print(f"[SB-RENDER] HTTP {resp.status_code} | {len(resp.text)} chars")

        if resp.status_code != 200:
            print(f"[SB-RENDER] Erro: {resp.text[:300]}")
            return []

        html = resp.text

        # Extrai dados do elemento que o JS injetou
        match = re.search(r'id="__scraped__"[^>]*>(.+?)</div>', html, re.DOTALL)
        if not match:
            print("[SB-RENDER] Elemento __scraped__ não encontrado no HTML")
            return []

        try:
            data = json.loads(match.group(1))
        except Exception as e:
            print(f"[SB-RENDER] Erro ao parsear JSON: {e}")
            return []

        if "error" in data:
            print(f"[SB-RENDER] Erro do JS: {data['error']}")
            return []

        products = extract_from_json(data, shop_id_str)
        print(f"[SB-RENDER] {len(products)} produtos encontrados")
        return products


def calcular_recebido(preco: float) -> float:
    """
    Fórmula exata Shopee Brasil:
    - Todos os produtos: desconta 20% (comissão + taxas)
    - Produtos até R$ 79,90: desconta R$ 4,00 fixo (frete subsidiado)
    - Produtos acima de R$ 79,90: sem taxa fixa de frete
    """
    taxa_pct = preco * 0.20
    taxa_fixa = 4.00 if preco <= 79.90 else 0.00
    return round(preco - taxa_pct - taxa_fixa, 2)


def calcular_margens(p: dict) -> dict:
    preco = p["preco"]
    sold = p["vendas_30d"]
    recebido = calcular_recebido(preco)

    def custo_max(margem_pct):
        # lucro líquido = recebido - custo
        # margem = lucro / preco_venda
        # custo_max = recebido - (preco * margem_pct)
        val = recebido - preco * margem_pct
        return round(val, 2) if val > 0 else 0.0

    return {
        "nome": p["nome"],
        "preco": preco,
        "vendas_30d": sold,
        "avaliacao": p.get("avaliacao", 0.0),
        "faturamento_30d": round(preco * sold, 2),
        "recebido_apos_taxas": recebido,
        "custo_max_20pct": custo_max(0.20),
        "custo_max_25pct": custo_max(0.25),
        "custo_max_30pct": custo_max(0.30),
        "vendas_por_dia": round(sold / 30, 1),
    }


def gerar_insights(products, total_fat):
    insights = []
    if not products:
        return insights

    top3 = products[:3]
    fat_top3 = sum(p["faturamento_30d"] for p in top3)
    pct = (fat_top3 / total_fat * 100) if total_fat > 0 else 0
    nomes = ", ".join(p["nome"][:30] for p in top3[:2])
    insights.append({
        "tipo": "info",
        "titulo": f"Top 3 produtos = {pct:.0f}% do faturamento",
        "descricao": f"{nomes} lideram as vendas e respondem por {pct:.0f}% do faturamento estimado.",
    })

    best = products[0]
    if best["vendas_por_dia"] > 0:
        insights.append({
            "tipo": "positivo",
            "titulo": f'"{best["nome"][:40]}" vende {best["vendas_por_dia"]:.1f}x por dia',
            "descricao": (
                f'Vende a R${best["preco"]:.2f} → você recebe R${best["recebido_apos_taxas"]:.2f} após taxas. '
                f'Para 20% de lucro líquido: compre por até R${best["custo_max_20pct"]:.2f}. '
                f'Para 25%: até R${best["custo_max_25pct"]:.2f}.'
            ),
        })

    alta = [p for p in products if p["vendas_30d"] >= 100]
    if alta:
        insights.append({
            "tipo": "atencao",
            "titulo": f"{len(alta)} produto(s) com 100+ vendas no mês",
            "descricao": "Alta demanda — boa oportunidade para copiar.",
        })

    baixa = [p for p in products if 5 <= p["vendas_30d"] <= 30]
    if baixa:
        insights.append({
            "tipo": "info",
            "titulo": f"{len(baixa)} produto(s) com vendas moderadas",
            "descricao": "5-30 vendas/mês costumam ter menos concorrência. Bom ponto de entrada.",
        })

    return insights


async def scrape_and_analyze(url: str) -> dict:
    store_url, username = clean_store_url(url)

    print(f"[INFO] Analisando loja: {username}")
    shop_id, shop_name = await get_shop_id(username)
    if not shop_id:
        raise ValueError("Loja não encontrada. Verifique o link.")
    shop_id_str = str(shop_id)
    print(f"[INFO] shop_id={shop_id_str}, nome={shop_name}")

    # Estratégia 1: ScrapingBee chamando a API diretamente (1 crédito)
    raw_products = await fetch_via_scrapingbee_api(shop_id_str, username)

    # Estratégia 2: ScrapingBee renderizando a página + JS injetado (5 créditos)
    if not raw_products:
        raw_products = await fetch_via_scrapingbee_render(shop_id_str, username, store_url)

    if not raw_products:
        raise ValueError(
            "Não foi possível carregar os produtos desta loja. Tente novamente."
        )

    # Remove duplicatas
    seen = set()
    unique = []
    for p in raw_products:
        key = p["nome"].lower()[:50]
        if key not in seen:
            seen.add(key)
            unique.append(calcular_margens(p))

    unique.sort(key=lambda x: x["faturamento_30d"], reverse=True)

    total_fat = sum(p["faturamento_30d"] for p in unique)
    total_vendas = sum(p["vendas_30d"] for p in unique)

    print(f"[OK] {len(unique)} produtos | faturamento R${total_fat:.2f}")

    return {
        "loja": shop_name,
        "url": store_url,
        "total_produtos": len(unique),
        "faturamento_30d": round(total_fat, 2),
        "total_vendas_30d": total_vendas,
        "melhor_produto": unique[0] if unique else None,
        "produtos": unique,
        "insights": gerar_insights(unique, total_fat),
    }
