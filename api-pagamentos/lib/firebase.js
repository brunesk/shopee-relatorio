// Firebase Admin — credencial via env FIREBASE_SERVICE_ACCOUNT_B64
// (JSON da service account codificado em base64, gerado no console do Firebase:
//  Configurações do projeto → Contas de serviço → Gerar nova chave privada)
const admin = require('firebase-admin')

if (!admin.apps.length) {
  const json = Buffer.from(process.env.FIREBASE_SERVICE_ACCOUNT_B64, 'base64').toString('utf8')
  admin.initializeApp({ credential: admin.credential.cert(JSON.parse(json)) })
}

module.exports = { db: admin.firestore(), admin }
