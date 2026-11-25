# PNCP & B2G Health Radar Runner

Runner público **solo de código** (sin datos, Excel ni credenciales en el repositorio) diseñado para ejecutarse gratis en **GitHub Actions** cada **2 horas** (`0 */2 * * *`) y mantener sincronizada una planilla privada de **Google Sheets**.

Qué consulta en cada corrida:
- **PNCP Brasil** (`pncp.gov.br/api/search` y `/api/consulta/v1/...`): detecta procesos abiertos de telessaúde, telemedicina, teleconsulta, prontuario electrónico y software de salud; refresca montos, cierres y estados (`Suspensa`, `Encerrada`) sin pisar dictámenes manuales.
- **TCE-RS LicitaCon 2026** (`dados.tce.rs.gov.br/dados/licitacon/licitacao/ano/2026.csv.zip`): extrae por HTTP `Range` únicamente `licitacao.csv` (~24 MB en lugar de 282 MB), filtrando licitaciones y credenciamentos activos/recientes de salud digital en Rio Grande do Sul hacia la pestaña `TCE_RS_2026`.
- **TCE-RS CKAN** (`dados.tce.rs.gov.br`) y **Boletín Oficial de Argentina (3.ª Sección)**.

---

## Configuración en 3 pasos (2 minutos)

### Paso 1: Subir el Excel a Google Sheets (queda privado en tu Google Drive)
1. Importá tu archivo `.xlsx` de radar en Google Drive y abrilo con **Hojas de cálculo de Google (Google Sheets)** (o creá una hoja nueva vacía; el script crea las pestañas necesarias automáticamente).
2. Copiá el **`GOOGLE_SHEET_ID`** desde la URL de la planilla:
   `https://docs.google.com/spreadsheets/d/<GOOGLE_SHEET_ID>/edit`

### Paso 2: Crear Service Account en Google Cloud
1. En Google Cloud Console, habilitá **Google Sheets API**.
2. Creá una **Service Account** y descargá su clave en formato **JSON**.
3. En tu Google Sheet privado, tocá **Compartir** y agregá el **`client_email`** de esa Service Account con rol **Editor**.

### Paso 3: Cargar Secrets en GitHub
Andá a este repositorio → **`Settings → Secrets and variables → Actions → New repository secret`** y agregá:
- `GOOGLE_SERVICE_ACCOUNT_JSON`: el contenido completo del archivo JSON de la Service Account.
- `GOOGLE_SHEET_ID`: el ID copiado en el Paso 1.
- *(Opcional — alertas en vivo)*: `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.

A partir de ese momento, cada corrida automática cada 2 horas (o manual desde la pestaña **Actions**) actualiza en vivo las pestañas `Oportunidades_PNCP_BR`, `TCE_RS_2026`, `Resumen_Ejecutivo` y `Log_Sincronizacion` de tu Google Sheet privado.
