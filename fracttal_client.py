import os
import re
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


FRACTTAL_URL = os.environ.get("FRACTTAL_URL", "https://app.fracttal.com/signin")
FRACTTAL_USER = os.environ.get("FRACTTAL_USER", "seguridad.integral@spsa.pe")
FRACTTAL_PASSWORD = os.environ.get("FRACTTAL_PASSWORD", "Sfh2026#")


def login_fracttal(page):
    page.set_default_timeout(30000)
    page.goto(FRACTTAL_URL, wait_until="domcontentloaded", timeout=60000)

    print("URL inicial:", page.url)
    print("Título:", page.title())

    page.wait_for_timeout(5000)

    email_locator = page.locator(
        'input[type="email"], input[name="email"], input[placeholder*="correo" i], input[placeholder*="email" i]'
    ).first
    email_locator.wait_for(timeout=20000)
    email_locator.fill(FRACTTAL_USER)
    print("Correo ingresado")

    try:
        submit_locator = page.locator(
            'button[type="submit"], input[type="submit"], button, [role="button"]'
        ).filter(
            has_text=re.compile(r"continuar|siguiente|ingresar|iniciar|login|acceder", re.IGNORECASE)
        ).first
        submit_locator.wait_for(timeout=5000)
        submit_locator.click()
        print("Click después de correo")
    except Exception:
        print("No se encontró botón después de correo, usando Enter")
        email_locator.press("Enter")

    page.wait_for_timeout(5000)

    password_locator = page.locator(
        'input[type="password"], input[name="password"], input[placeholder*="contraseña" i], input[placeholder*="password" i]'
    ).first
    password_locator.wait_for(timeout=20000)
    password_locator.fill(FRACTTAL_PASSWORD)
    print("Password ingresado")

    try:
        submit_locator = page.locator(
            'button[type="submit"], input[type="submit"], button, [role="button"]'
        ).filter(
            has_text=re.compile(r"continuar|siguiente|ingresar|iniciar|login|acceder", re.IGNORECASE)
        ).first
        submit_locator.wait_for(timeout=5000)
        submit_locator.click()
        print("Click final login")
    except Exception:
        print("No se encontró botón final de login, usando Enter")
        password_locator.press("Enter")

    page.wait_for_timeout(8000)

    current_url = page.url
    final_html = page.content().lower()

    print("URL final login:", current_url)

    login_ok = (
        "dashboard" in current_url.lower()
        or "solicitudes" in final_html
        or "dashboard" in final_html
        or "inicio" in final_html
    )

    return login_ok


def navigate_to_work_requests():
    if not FRACTTAL_USER or not FRACTTAL_PASSWORD:
        return {
            "ok": False,
            "message": "Faltan variables de entorno de Fracttal."
        }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            page = browser.new_page(viewport={"width": 1900, "height": 950})

            login_ok = login_fracttal(page)
            if not login_ok:
                browser.close()
                return {
                    "ok": False,
                    "message": "No se pudo iniciar sesión en Fracttal.",
                    "url_final": page.url
                }

            page.wait_for_timeout(5000)
            page.locator("text=Dashboard").first.wait_for(timeout=15000)
            print("Dashboard visible")

            # Buscar botón directo del dashboard hacia Work Requests
            work_requests_btn = page.locator(
                '[aria-label*="Go to"][aria-label*="Work Requests"]'
            ).first

            work_requests_btn.wait_for(timeout=10000)
            work_requests_btn.click()
            page.wait_for_timeout(6000)

            current_url = page.url
            current_text = page.locator("body").inner_text(timeout=10000).lower()

            print("URL final:", current_url)
            print("BODY final preview:", current_text[:2500])

            page_ok = (
                "work requests" in current_text
                or "created" in current_text
                or "solved" in current_text
            )

            browser.close()

            if page_ok:
                return {
                    "ok": True,
                    "message": "Navegación exitosa hacia Work Requests.",
                    "url_final": current_url
                }

            return {
                "ok": False,
                "message": "Se hizo click en Work Requests, pero no se pudo confirmar el cambio de vista.",
                "url_final": current_url
            }

    except PlaywrightTimeoutError as e:
        return {
            "ok": False,
            "message": f"Timeout durante la navegación en Fracttal: {str(e)}"
        }
    except Exception as e:
        import traceback
        return {
            "ok": False,
            "message": f"Error navegando a Work Requests: {str(e)}",
            "trace": traceback.format_exc()
        }