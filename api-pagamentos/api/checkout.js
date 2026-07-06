// POST /api/checkout { email } → { url } (link do Mercado Pago)
const PRODUTO = {
  titulo: 'Acelera Shopee Gestão Financeira — Acesso 12 meses',
  preco: 149.9,
}

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
}

module.exports = async (req, res) => {
  Object.entries(CORS).forEach(([k, v]) => res.setHeader(k, v))
  if (req.method === 'OPTIONS') return res.status(204).end()
  if (req.method !== 'POST') return res.status(405).json({ error: 'method not allowed' })

  const email = String(req.body?.email ?? '').toLowerCase().trim()
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    return res.status(400).json({ error: 'Email inválido' })
  }

  const siteUrl = process.env.SITE_URL // ex: https://brunesk.github.io/shopee-relatorio
  const apiUrl  = process.env.API_URL  // ex: https://acelera-pagamentos.vercel.app

  try {
    const r = await fetch('https://api.mercadopago.com/checkout/preferences', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${process.env.MP_ACCESS_TOKEN}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        items: [{
          id: 'acelera-gf-12m',
          title: PRODUTO.titulo,
          quantity: 1,
          currency_id: 'BRL',
          unit_price: PRODUTO.preco,
        }],
        payer: { email },
        metadata: { account_email: email },
        external_reference: email,
        back_urls: {
          success: `${siteUrl}/?pago=1`,
          pending: `${siteUrl}/?pago=1`,
          failure: `${siteUrl}/?pago=erro`,
        },
        auto_return: 'approved',
        notification_url: `${apiUrl}/api/webhook`,
        statement_descriptor: 'ACELERASHOPEE',
      }),
    })

    if (!r.ok) {
      console.error('MP preference:', r.status, await r.text())
      return res.status(502).json({ error: 'Falha ao criar pagamento' })
    }
    const pref = await r.json()
    return res.status(200).json({ url: pref.init_point })
  } catch (e) {
    console.error('checkout:', e)
    return res.status(500).json({ error: 'Erro interno' })
  }
}
