"""
core/html_builder.py
Budowanie HTML dla maili odpowiedzi.
Z pastelowymikolorami i pełnym tłem obejmującym cały email.
"""


def build_html_reply(body_text: str) -> str:
    """
    Formatuje tekst jako HTML z pastelowymikolorami.
    Tło obejmuje cały email (gradient z pastelowymi kolorami).
    """
    body_text = body_text.replace("\n", "<br>")
    
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{
            margin: 0;
            padding: 20px;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #FFE4E1 0%, #E0F4FF 25%, #E8F5E9 50%, #FFF9C4 75%, #FCE4EC 100%);
            min-height: 100vh;
        }}
        .container {{
            max-width: 600px;
            margin: 0 auto;
            background: rgba(255, 255, 255, 0.95);
            border-radius: 12px;
            padding: 30px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
            border: 2px solid rgba(200, 220, 255, 0.3);
        }}
        .header {{
            background: linear-gradient(135deg, #B3E5FC 0%, #C8E6C9 100%);
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            border-left: 5px solid #81C784;
        }}
        .content {{
            font-size: 15px;
            line-height: 1.8;
            color: #333;
        }}
        .content p {{
            margin: 15px 0;
            color: #444;
        }}
        .content i {{
            color: #7B68EE;
            font-style: italic;
        }}
        .footer {{
            margin-top: 30px;
            padding-top: 20px;
            border-top: 2px solid #FFE0B2;
            font-size: 12px;
            color: #0a8a0a;
            text-align: center;
            background: linear-gradient(to bottom, transparent, rgba(255, 224, 178, 0.2));
            border-radius: 6px;
            padding: 15px;
        }}
        .footer a {{
            color: #0a8a0a;
            text-decoration: none;
            border-bottom: 1px dotted #0a8a0a;
        }}
        .footer a:hover {{
            border-bottom: 1px solid #0a8a0a;
        }}
        .signature {{
            color: #7B68EE;
            font-weight: 600;
            margin-top: 10px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <p style="margin: 0; color: #333; font-weight: 500;">
                ✉️ Odpowiedź automatyczna
            </p>
        </div>
        
        <div class="content">
            <p><i>{body_text}</i></p>
        </div>
        
        <div class="footer">
            <p style="margin: 0 0 10px 0;">
                Odpowiedź wygenerowana automatycznie przez system Script + Render.<br>
                <span style="font-size: 11px; color: #088a08;">
                    🔗 Projekt dostępny na GitHub:<br>
                    <a href="https://github.com/legionowopawel/AutoResponder_AI_Text.git">
                        AutoResponder_AI_Text
                    </a>
                </span>
            </p>
        </div>
    </div>
</body>
</html>"""
    
    return html


def build_html_reply_minimal(body_text: str) -> str:
    """
    Wersja minimalistyczna — tylko tekst z pastelowymikolorami tła.
    Szybka i lekka dla szybszych emaili.
    """
    body_text = body_text.replace("\n", "<br>")
    
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <style>
        body {{
            margin: 0;
            padding: 15px;
            background: linear-gradient(135deg, #FFE4E1 0%, #E0F4FF 50%, #FFF9C4 100%);
            font-family: Arial, sans-serif;
            font-size: 14px;
            color: #444;
        }}
        p {{ margin: 10px 0; }}
        i {{ color: #7B68EE; }}
    </style>
</head>
<body>
    <p><i>{body_text}</i></p>
    <p style="font-size: 11px; color: #0a8a0a; margin-top: 20px;">
        Odpowiedź wygenerowana automatycznie.<br>
        <a href="https://github.com/legionowopawel/AutoResponder_AI_Text.git" style="color: #0a8a0a; text-decoration: none;">
            Script + Render
        </a>
    </p>
</body>
</html>"""


def build_html_reply_dark(body_text: str) -> str:
    """
    Wersja temna z pastelowymi akcentami na ciemnym tle.
    Dla wieczorowego czytania.
    """
    body_text = body_text.replace("\n", "<br>")
    
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <style>
        body {{
            margin: 0;
            padding: 20px;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            font-family: 'Segoe UI', sans-serif;
            color: #e0e0e0;
            min-height: 100vh;
        }}
        .container {{
            max-width: 600px;
            margin: 0 auto;
            background: rgba(30, 30, 50, 0.9);
            border-radius: 10px;
            padding: 25px;
            border: 2px solid #B3E5FC;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
        }}
        .content {{
            color: #f5f5f5;
            font-size: 15px;
            line-height: 1.7;
        }}
        .content i {{
            color: #FFB6C1;
            text-decoration: underline;
        }}
        .footer {{
            margin-top: 25px;
            padding-top: 15px;
            border-top: 1px solid #B3E5FC;
            font-size: 11px;
            color: #90CAF9;
            text-align: center;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="content">
            <p><i>{body_text}</i></p>
        </div>
        <div class="footer">
            Odpowiedź automatyczna | Script + Render<br>
            <a href="https://github.com/legionowopawel/AutoResponder_AI_Text.git" style="color: #90CAF9; text-decoration: none;">
                GitHub
            </a>
        </div>
    </div>
</body>
</html>"""


def wrap_with_background(html_content: str, color_scheme: str = "pastel") -> str:
    """
    Opakuj dowolny HTML w pastelowe tło obejmujące cały email.
    
    Args:
        html_content: Zawartość HTML do opakowania
        color_scheme: "pastel", "minimal", "dark", "sunset", "ocean"
    
    Returns:
        Pełny HTML z tłem
    """
    
    schemes = {
        "pastel": "linear-gradient(135deg, #FFE4E1 0%, #E0F4FF 25%, #E8F5E9 50%, #FFF9C4 75%, #FCE4EC 100%)",
        "minimal": "linear-gradient(135deg, #F5F5F5 0%, #E8F5E9 50%, #FFF9C4 100%)",
        "dark": "linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%)",
        "sunset": "linear-gradient(135deg, #FFE4B5 0%, #FFB6C1 50%, #DDA0DD 100%)",
        "ocean": "linear-gradient(135deg, #B0E0E6 0%, #E0FFFF 50%, #B0E0E6 100%)",
    }
    
    bg = schemes.get(color_scheme, schemes["pastel"])
    
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{
            margin: 0;
            padding: 20px;
            background: {bg};
            font-family: 'Segoe UI', Arial, sans-serif;
            min-height: 100vh;
        }}
        * {{
            box-sizing: border-box;
        }}
    </style>
</head>
<body>
    {html_content}
</body>
</html>"""
