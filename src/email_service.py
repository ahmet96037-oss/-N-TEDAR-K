"""Email gönderme servisi — SMTP/Sendgrid."""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "noreply@tedarik.com")
SENDER_NAME = os.environ.get("SENDER_NAME", "Tedarik Ağı")


def send_email(
    to_email: str,
    subject: str,
    html_body: str,
    text_body: Optional[str] = None,
) -> bool:
    """SMTP aracılığıyla email gönder."""
    if not SMTP_USER or not SMTP_PASS:
        # SMTP ayarlanmamışsa, log'la ama hata verme (geliştirme modu)
        print(f"[EMAIL] {to_email} — {subject} (SMTP not configured)")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
        msg["To"] = to_email

        # Plain text fallback
        if text_body:
            msg.attach(MIMEText(text_body, "plain"))

        # HTML version (tercih edilen)
        msg.attach(MIMEText(html_body, "html"))

        # SMTP ile gönder
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SENDER_EMAIL, to_email, msg.as_string())

        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {to_email} — {subject}: {str(e)}")
        return False


def send_quote_confirmation(customer_name: str, customer_email: str, quote_number: str, total: float):
    """Teklif onay emaili gönder."""
    subject = f"Teklif Onayı — {quote_number}"
    html = f"""
    <html>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; padding: 20px;">
      <div style="max-width: 600px; margin: 0 auto; background: white; border-radius: 12px; padding: 40px; box-shadow: 0 2px 8px rgba(0,0,0,.1);">
        <h1 style="color: #1fae70; margin-bottom: 20px;">✅ Teklif Onaylandı</h1>
        <p>Merhaba <strong>{customer_name}</strong>,</p>
        <p>Teklifiniz başarıyla oluşturulmuştur ve ilişkili ekibimiz bunu işlemeye başlamıştır.</p>
        <div style="background: #f9f9ff; border-left: 4px solid #1fae70; padding: 15px; margin: 20px 0; border-radius: 4px;">
          <p><strong>Teklif No:</strong> {quote_number}</p>
          <p><strong>Toplam Tutar:</strong> ${total:.2f}</p>
        </div>
        <p>Müşteri panelinizdeki <a href="https://cin-tedarik-sistem.vercel.app/dashboard.html" style="color: #1fae70; text-decoration: none;">siparişlerim</a> bölümünden teklifi ve ilerlemeyi takip edebilirsiniz.</p>
        <p style="margin-top: 30px; color: #666; font-size: 12px;">
          © Tedarik Ağı — İthalat İhracat ve Gümrük Danışmanlığı
        </p>
      </div>
    </body>
    </html>
    """
    text = f"Teklif Onaylandı — {quote_number}\n\nMerhaba {customer_name},\n\nTeklifiniz başarıyla oluşturulmuştur.\n\nTeklif No: {quote_number}\nToplam: ${total:.2f}\n\nDashboard'unuzda detayları görebilirsiniz."
    return send_email(customer_email, subject, html, text)


def send_order_shipped(customer_name: str, customer_email: str, order_number: str, tracking_number: str):
    """Sipariş gönderim bildirimi."""
    subject = f"Siparişiniz Gönderildi — {order_number}"
    html = f"""
    <html>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; padding: 20px;">
      <div style="max-width: 600px; margin: 0 auto; background: white; border-radius: 12px; padding: 40px; box-shadow: 0 2px 8px rgba(0,0,0,.1);">
        <h1 style="color: #1fae70; margin-bottom: 20px;">🚚 Siparişiniz Gönderildi</h1>
        <p>Merhaba <strong>{customer_name}</strong>,</p>
        <p>Siparişiniz yola çıkmıştır! Aşağıdaki bilgileri kullanarak takip edebilirsiniz.</p>
        <div style="background: #f9f9ff; border-left: 4px solid #1fae70; padding: 15px; margin: 20px 0; border-radius: 4px;">
          <p><strong>Sipariş No:</strong> {order_number}</p>
          <p><strong>Takip No:</strong> {tracking_number}</p>
        </div>
        <p>
          <a href="https://cin-tedarik-sistem.vercel.app/dashboard.html" style="display: inline-block; background: #1fae70; color: white; padding: 12px 24px; border-radius: 8px; text-decoration: none; font-weight: 600;">
            Sipariş Detaylarını Gör
          </a>
        </p>
        <p style="margin-top: 30px; color: #666; font-size: 12px;">
          © Tedarik Ağı — İthalat İhracat ve Gümrük Danışmanlığı
        </p>
      </div>
    </body>
    </html>
    """
    text = f"Siparişiniz Gönderildi — {order_number}\n\nMerhaba {customer_name},\n\nSipariş No: {order_number}\nTakip No: {tracking_number}\n\nDashboard'unuzdan takip edebilirsiniz."
    return send_email(customer_email, subject, html, text)


def send_delivery_confirmed(customer_name: str, customer_email: str, order_number: str):
    """Teslimat onay bildirimi."""
    subject = f"Siparişiniz Teslim Edildi — {order_number}"
    html = f"""
    <html>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; padding: 20px;">
      <div style="max-width: 600px; margin: 0 auto; background: white; border-radius: 12px; padding: 40px; box-shadow: 0 2px 8px rgba(0,0,0,.1);">
        <h1 style="color: #1fae70; margin-bottom: 20px;">🎉 Siparişiniz Teslim Edildi</h1>
        <p>Merhaba <strong>{customer_name}</strong>,</p>
        <p>Tebrikler! Siparişiniz başarıyla teslim edilmiştir.</p>
        <div style="background: #f9f9ff; border-left: 4px solid #1fae70; padding: 15px; margin: 20px 0; border-radius: 4px;">
          <p><strong>Sipariş No:</strong> {order_number}</p>
          <p><strong>Durum:</strong> ✅ Teslim Edildi</p>
        </div>
        <p>Herhangi bir sorunuz varsa veya yardıma ihtiyacınız olursa, bize ulaşmaktan çekinmeyin.</p>
        <p style="margin-top: 30px; color: #666; font-size: 12px;">
          © Tedarik Ağı — İthalat İhracat ve Gümrük Danışmanlığı
        </p>
      </div>
    </body>
    </html>
    """
    text = f"Siparişiniz Teslim Edildi — {order_number}\n\nMerhaba {customer_name},\n\nSipariş No: {order_number}\nDurum: ✅ Teslim Edildi"
    return send_email(customer_email, subject, html, text)


def send_notification_email(customer_name: str, customer_email: str, notification_type: str, subject: str, message: str):
    """Generic bildirim emaili."""
    emoji_map = {
        "quote_created": "📄",
        "order_shipped": "🚚",
        "tracking_update": "📍",
        "delivery_confirm": "✅",
    }
    emoji = emoji_map.get(notification_type, "📬")

    html = f"""
    <html>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; padding: 20px;">
      <div style="max-width: 600px; margin: 0 auto; background: white; border-radius: 12px; padding: 40px; box-shadow: 0 2px 8px rgba(0,0,0,.1);">
        <h1 style="color: #1fae70; margin-bottom: 20px;">{emoji} {subject}</h1>
        <p>Merhaba <strong>{customer_name}</strong>,</p>
        <p>{message}</p>
        <p>
          <a href="https://cin-tedarik-sistem.vercel.app/dashboard.html" style="display: inline-block; background: #1fae70; color: white; padding: 12px 24px; border-radius: 8px; text-decoration: none; font-weight: 600; margin-top: 20px;">
            Panele Git
          </a>
        </p>
        <p style="margin-top: 30px; color: #666; font-size: 12px;">
          © Tedarik Ağı
        </p>
      </div>
    </body>
    </html>
    """
    return send_email(customer_email, subject, html, message)
