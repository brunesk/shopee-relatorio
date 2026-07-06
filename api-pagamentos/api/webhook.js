// Webhook do Mercado Pago → grava licença no Firestore
// approved → licencas/{email} ativa por 12 meses (renovação soma ao prazo atual)
// refunded / cancelled / charged_back → licença desativada
const { db, admin } = require('../lib/firebase')

const MESES_ACESSO = 12

module.exports = async (req, res) => {
  // MP manda o id do pagamento na query (?type=payment&data.id=X ou ?topic=payment&id=X) ou no corpo
  const q = req.query || {}
  let type = q.type || q.topic
  let paymentId = q['data.id'] || (q.topic === 'payment' ? q.id : null)

  if (!paymentId && req.body) {
    type = req.body.type || req.body.topic || type
    paymentId = req.body?.data?.id || req.body?.resource || null
  }

  if (type !== 'payment' || !paymentId) return res.status(200).json({ ignored: true })

  // Nunca confiar no corpo: busca o status real na API do MP
  const r = await fetch(`https://api.mercadopago.com/v1/payments/${paymentId}`, {
    headers: { Authorization: `Bearer ${process.env.MP_ACCESS_TOKEN}` },
  })
  if (!r.ok) return res.status(500).json({ error: 'payment lookup failed' }) // MP reenvia depois

  const pay = await r.json()
  const email = String(pay.metadata?.account_email || pay.external_reference || pay.payer?.email || '')
    .toLowerCase().trim()
  if (!email) return res.status(200).json({ ignored: true })

  const ref = db.collection('licencas').doc(email)

  if (pay.status === 'approved') {
    const pagoEm = pay.date_approved ? new Date(pay.date_approved) : new Date()

    // Renovação: se a licença atual ainda vale, soma 12 meses ao vencimento existente
    const atual = await ref.get()
    let base = pagoEm
    if (atual.exists) {
      const l = atual.data()
      if (l.status === 'ativa' && l.expiraEm && l.expiraEm.toDate() > pagoEm) {
        base = l.expiraEm.toDate()
      }
    }
    const expira = new Date(base)
    expira.setMonth(expira.getMonth() + MESES_ACESSO)

    await ref.set({
      status: 'ativa',
      plano: 'anual',
      valor: pay.transaction_amount,
      mpPaymentId: String(pay.id),
      pagoEm: admin.firestore.Timestamp.fromDate(pagoEm),
      expiraEm: admin.firestore.Timestamp.fromDate(expira),
      atualizadoEm: admin.firestore.FieldValue.serverTimestamp(),
    }, { merge: true })

    console.log('licença ativada:', email, '→', expira.toISOString())
    return res.status(200).json({ ok: true, activated: email })
  }

  if (['refunded', 'cancelled', 'charged_back'].includes(pay.status)) {
    await ref.set({
      status: 'cancelada',
      motivo: pay.status,
      atualizadoEm: admin.firestore.FieldValue.serverTimestamp(),
    }, { merge: true })
    return res.status(200).json({ ok: true, deactivated: email })
  }

  return res.status(200).json({ ok: true, status: pay.status })
}
