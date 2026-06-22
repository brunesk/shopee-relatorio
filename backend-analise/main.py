import asyncio
import httpx
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright

app = FastAPI(title="Shopee Store Analyzer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalyzeRequest(BaseModel):
    url: str


@app.get("/")
async def root():
    return {"status": "ok", "message": "Shopee Analyzer API funcionando"}


@app.post("/analisar")
async def analisar(req: AnalyzeRequest):
    try:
        resultado = await scrape_and_analyze(req.url)
        return resultado
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
        raise ValueError("Link inválido. Use o formato: https://shopee.com.br/nomeddaloja")
    username = parts[0]
    return f"https://shopee.com.br/{username}", username


async def get_shop_info(username: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        "Referer": f"https://shopee.com.br/{username}",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"https://shopee.com.br/api/v4/shop/get_shop_detail?username={username}",
            headers=headers,
        )
        data = resp.json()
        shop_data = data.get("data") or {}
        if isinstance(shop_data, dict):
            return shop_data.get("name") or username
        return username


def gerar_insights(products: list, total_fat: float) -> list:
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
                f'Para entrar com 30% de margem: compre por até R${best["preco_compra_30pct"]:.2f}. '
                f'Para 40% de margem: até R${best["preco_compra_40pct"]:.2f}. '
                f'Preço de venda atual: R${best["preco"]:.2f}.'
            ),
        })

    alta_vel = [p for p in products if p["vendas_30d"] >= 100]
    if alta_vel:
        insights.append({
            "tipo": "atencao",
            "titulo": f"{len(alta_vel)} produto(s) com 100+ vendas no mês",
            "descricao": "Produtos com alto volume indicam forte demanda. São boas oportunidades para copiar.",
        })

    baixa_vel = [p for p in products if 5 <= p["vendas_30d"] <= 30]
    if baixa_vel:
        insights.append({
            "tipo": "info",
            "titulo": f"{len(baixa_vel)} produto(s) com vendas moderadas — menos concorrência",
            "descricao": "Produtos com 5-30 vendas/mês podem ter menos disputa. Bom ponto de entrada.",
        })

    return insights


async def scrape_and_analyze(url: str) -> dict:
    store_url, username = clean_store_url(url)
    sorted_url = store_url + "?page=0&sortBy=sales&tab=0"

    print(f"[SCRAPER] Buscando info da loja: {username}")
    shop_name = await get_shop_info(username)
    print(f"[SCRAPER] Loja: {shop_name}")

    products = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-setuid-sandbox",
            ],
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
        )

        page = await context.new_page()

        # Homepage primeiro para cookies de sessão
        print("[SCRAPER] Obtendo cookies...")
        try:
            await page.goto("https://shopee.com.br/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
        except Exception as e:
            print(f"[AVISO] Homepage: {e}")

        print(f"[SCRAPER] Acessando: {sorted_url}")
        try:
            await page.goto(sorted_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_load_state("load", timeout=30000)
        except Exception as e:
            print(f"[AVISO] Load: {e}")

        # Aguarda renderização e rola a página para carregar todos os produtos
        await asyncio.sleep(8)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.3)")
        await asyncio.sleep(2)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.6)")
        await asyncio.sleep(2)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(3)
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(2)

        # Extrai produtos diretamente do DOM renderizado
        print("[SCRAPER] Extraindo do DOM...")
        dom_products = await page.evaluate("""
            () => {
                const products = [];
                const seen = new Set();

                // Percorre todos os nós de texto procurando "X vendas"
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                const vendaNodes = [];
                let node;
                while (node = walker.nextNode()) {
                    const t = node.textContent.trim();
                    if (/^\\d[\\d.]*\\s+vendas?$/i.test(t)) {
                        vendaNodes.push(node);
                    }
                }

                console.log('Nos vendas encontrados: ' + vendaNodes.length);

                vendaNodes.forEach(vendaNode => {
                    // Sobe na árvore para achar o card do produto
                    let el = vendaNode.parentElement;
                    let card = null;
                    for (let i = 0; i < 15; i++) {
                        if (!el || el === document.body) break;
                        const links = el.querySelectorAll('a[href]');
                        if (links.length >= 1 && el.innerText && el.innerText.length > 15) {
                            card = el;
                            break;
                        }
                        el = el.parentElement;
                    }
                    if (!card) return;

                    const allText = card.innerText || '';

                    // Preço: padrão R$ XX,XX ou R$XX,XX
                    const priceMatch = allText.match(/R\\$\\s*([\\d.]+,[\\d]{2})/);
                    const price = priceMatch
                        ? parseFloat(priceMatch[1].replace(/\\./g, '').replace(',', '.'))
                        : 0;

                    // Vendas
                    const salesMatch = allText.match(/(\\d[\\d.]*?)\\s+vendas?/i);
                    const sold = salesMatch
                        ? parseInt(salesMatch[1].replace(/\\./g, ''), 10)
                        : 0;

                    // Nome: primeira linha relevante
                    const lines = allText.split('\\n')
                        .map(l => l.trim())
                        .filter(l =>
                            l.length > 8 &&
                            !l.match(/^R\\$/) &&
                            !l.match(/vendas?/i) &&
                            !l.match(/^[\\d.,]+$/) &&
                            !l.match(/^\\d+%/) &&
                            !l.match(/^\\*+/) &&
                            !l.match(/^[⭐★]+/)
                        );
                    const name = lines[0] || '';

                    const key = name.toLowerCase().substring(0, 40) + '_' + price;
                    if (name && price > 0 && !seen.has(key)) {
                        seen.add(key);
                        products.push({ nome: name, preco: price, vendas_30d: sold });
                    }
                });

                return products;
            }
        """)

        print(f"[DOM] {len(dom_products)} produtos encontrados")
        await browser.close()

    # Calcula margens e métricas
    for p in dom_products:
        preco = p["preco"]
        sold = p["vendas_30d"]
        recebido = preco * 0.80  # 20% taxa Shopee
        p["historico_vendas"] = 0
        p["avaliacao"] = 0.0
        p["faturamento_30d"] = round(preco * sold, 2)
        p["preco_compra_30pct"] = round(recebido * 0.70, 2)
        p["preco_compra_40pct"] = round(recebido * 0.60, 2)
        p["vendas_por_dia"] = round(sold / 30, 1)

    products = dom_products

    if not products:
        raise ValueError(
            "Nenhum produto encontrado. Verifique se o link é de uma loja Shopee válida e tente novamente."
        )

    products.sort(key=lambda x: x["faturamento_30d"], reverse=True)

    total_fat = sum(p["faturamento_30d"] for p in products)
    total_vendas = sum(p["vendas_30d"] for p in products)

    return {
        "loja": shop_name,
        "url": store_url,
        "total_produtos": len(products),
        "faturamento_30d": round(total_fat, 2),
        "total_vendas_30d": total_vendas,
        "melhor_produto": products[0] if products else None,
        "produtos": products,
        "insights": gerar_insights(products, total_fat),
    }
