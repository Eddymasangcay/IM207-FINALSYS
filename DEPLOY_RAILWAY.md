# Railway Go-Live Checklist

## 1) Create Railway services
- Create a new Railway project.
- Add a PostgreSQL plugin/service.
- Deploy this repo as a web service.

## 2) Configure Railway variables
Set these in the Railway service variables panel:
- `DATABASE_URL` (from Railway PostgreSQL)
- `PAYMONGO_SECRET_KEY` (`sk_live_...`)
- `PAYMONGO_WEBHOOK_SECRET` (`whsk_...`)
- `PUBLIC_APP_URL` (`https://your-domain.com`)
- `FLASK_SECRET_KEY` (new random secret)
- `PAYMONGO_PAYMENT_METHOD_TYPES` (for example: `card,gcash,paymaya`)
- `USD_TO_PHP_RATE` (for example: `56.0`)

## 3) Runtime/startup
- Build installs from `requirements.txt`.
- Start command uses `Procfile`: `web: gunicorn main:app`.

## 4) PayMongo dashboard configuration
- Switch to live mode.
- Confirm live payment methods are enabled.
- Create webhook:
  - URL: `https://your-domain.com/webhooks/paymongo`
  - Events: `checkout_session.payment.paid` (recommended), optionally `payment.failed`
- Copy webhook secret and set it as `PAYMONGO_WEBHOOK_SECRET` in Railway.

## 5) Smoke tests
- Open site over HTTPS and log in.
- Start a new rental checkout and pick PayMongo.
- Complete a low-value real transaction.
- Confirm:
  - redirect back succeeds
  - receipt page opens
  - payment reference is stored
  - booking is marked paid/confirmed

## 6) Idempotency and retries
- Re-send the same webhook event from PayMongo dashboard (or wait for retry behavior).
- Confirm no duplicate booking is created.

## 7) Post go-live security
- Rotate any previously exposed keys.
- Keep `.env` local only; never commit real secrets.
- Monitor Railway logs and PayMongo webhook delivery logs for the first 24 hours.
