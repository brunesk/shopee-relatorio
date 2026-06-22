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

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


class AnalyzeRequest(BaseModel):
    url: str


@app.get("/")
async def root():
    return {"status": "ok", "message": "Shopee Analyzer API funcionando"}


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


def find_products_in_json(obj, depth=0):
    """Varre o JSON recursivamente e retorna todos os objetos com name+price."""
    if depth > 12:
        return []
    results = []
    if isinstance(obj, dict):
        name = obj.get("name") or obj.get("item_name") or ""
        price = obj.get("price") or obj.get("price_min") or obj.get("price_max") or 0
        sold = obj.get("sold") or 0
        shopid = obj.get("shopid") or obj.get("shop_id")
        if name and isinstance(name, str) and len(name) > 3 and isinstance(price, (int, float)) and price > 0:
            if price > 100000:
                price = price / 100000
            results.append({
                "nome": name.strip(),
                "preco": round(float(price), 2),
                "vendas_30d": int(sold),
                "_shopid": str(shopid) if shopid else "",
            })
        else:
            for v in obj.values():
                results.extend(find_products_in_json(v, depth + 1))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(find_products_in_json(item, depth + 1))
    return results


def calcular_margens(p: dict) -> dict:
    preco = p["preco"]
    sold = p["vendas_30d"]
    recebido = preco * 0.80
    return {
        "nome": p["nome"],
        "preco": preco,
        "vendas_30d": sold,
        "avaliacao": p.get("avaliacao", 0.0),
        "faturamento_30d": round(preco * sold, 2),
        "preco_compra_30pct": round(recebido * 0.70, 2),
        "preco_compra_40pct": round(recebido * 0.60, 2),
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
                f'Para entrar com 30% de margem: compre por até R${best["preco_compra_30pct"]:.2f}. '
                f'Para 40% de margem: até R${best["preco_compra_40pct"]:.2f}. '
                f'Preço de venda: R${best["preco"]:.2f}.'
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

    # 1. Pega shop_id via API pública
    print(f"[INFO] Buscando loja: {username}")
    shop_id, shop_name = await get_shop_id(username)
    if not shop_id:
        raise ValueError("Loja não encontrada. Verifique o link.")
    shop_id_str = str(shop_id)
    print(f"[INFO] shop_id={shop_id_str}, nome={shop_name}")

    sorted_url = store_url + "?page=0&sortBy=sales&tab=0"

    # 2. Intercepta respostas da Shopee enquanto a página carrega
    # A listagem da loja vai ter TODOS os itens com o mesmo shopid
    # Feeds de recomendação têm shopids misturados — descartamos eles
    captured_responses = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"],
        )
        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1366, "height": 900},
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
        )
        page = await context.new_page()

        async def handle_response(response):
            u = response.url
            if response.status == 200 and "shopee.com.br" in u and "/api/" in u:
                try:
                    ct = response.headers.get("content-type", "")
                    if "json" in ct:
                        data = await response.json()
                        captured_responses.append((u, data))
                except Exception:
                    pass

        page.on("response", handle_response)

        # Homepage para cookies
        print("[INFO] Carregando homepage...")
        try:
            await page.goto("https://shopee.com.br/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
        except Exception:
            pass

        # Página da loja
        print(f"[INFO] Carregando loja: {sorted_url}")
        try:
            await page.goto(sorted_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_load_state("load", timeout=30000)
        except Exception:
            pass

        await asyncio.sleep(8)
        # Scroll para forçar carregamento de todos os produtos
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.5)")
        await asyncio.sleep(3)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(3)

        await browser.close()

    print(f"[INFO] {len(captured_responses)} respostas JSON capturadas")

    # 3. Para cada resposta, extrai produtos e analisa os shopids
    # Estratégia: encontrar a resposta onde TODOS os produtos são desta loja
    shop_products = []

    for resp_url, data in captured_responses:
        items = find_products_in_json(data)
        if not items:
            continue

        shopids = set(i["_shopid"] for i in items if i["_shopid"])
        n_items = len(items)
        n_shop = sum(1 for i in items if i["_shopid"] == shop_id_str)

        print(f"[RESP] {n_items} itens | shopids únicos: {len(shopids)} | desta loja: {n_shop} | {resp_url[:70]}")

        # Resposta onde todos (ou quase todos) os itens são desta loja
        if n_shop > 0 and (n_shop == n_items or (n_shop / n_items) >= 0.8):
            candidates = [i for i in items if i["_shopid"] == shop_id_str]
            if len(candidates) > len(shop_products):
                shop_products = candidates
                print(f"[MATCH] Melhor listagem: {len(shop_products)} produtos da loja")

    # Fallback: se nenhuma resposta foi 80%+ desta loja, pega os itens pelo shopid
    if not shop_products:
        print("[FALLBACK] Usando todos os itens com shopid correto")
        all_items = []
        for _, data in captured_responses:
            all_items.extend(find_products_in_json(data))
        shop_products = [i for i in all_items if i["_shopid"] == shop_id_str]

    if not shop_products:
        raise ValueError(
            "Não foi possível carregar os produtos. Tente novamente em alguns instantes."
        )

    # 4. Remove duplicatas e calcula margens
    seen = set()
    unique = []
    for p in shop_products:
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
