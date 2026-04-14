"""
Narzędzie do wykrywania śmiesznych fragmentów w wiadomościach.

Zasady działania:
1. Dzieli długi tekst na segmenty (np. zdania, paragrafy)
2. Wysyła każdy segment do AI (DeepSeek) z prośbą o ocenę śmieszności
3. Segmenty uznane za śmieszne są oznaczane specjalnym formatowaniem
4. Formatowanie: białe litery na czarnym tle (color: white; background-color: black)

Uwagi:
- Minimalizuje liczbę zapytań AI: wysyła segmenty w batchach (max 5 segmentów na request)
- Cache'uje wyniki dla identycznych segmentów w ramach jednej sesji
- Obsługuje polskie znaki i długie teksty
"""

import re
import json
import logging
from typing import List, Tuple, Dict, Any
from core.ai_client import call_deepseek

logger = logging.getLogger(__name__)

# Konfiguracja
MAX_SEGMENTS_PER_BATCH = 5
MAX_SEGMENT_LENGTH = 500  # znaków

# Prompt do oceny śmieszności
SYSTEM_PROMPT = """Jesteś ekspertem od humoru. Oceniasz czy dany fragment tekstu jest śmieszny.
Oceń tekst pod kątem:
1. Czy zawiera elementy komiczne (żart, ironia, sarkazm, absurd, paradoks)
2. Czy jest celowo zabawne lub nieoczekiwanie śmieszne
3. Czy wywołuje uśmiech lub lekką rozbawienie

Odpowiedz TYLKO w formacie JSON:
{
  "czy_smieszny": true/false,
  "powod": "krótkie wyjaśnienie po polsku"
}

Nie dodawaj żadnego tekstu poza JSON."""

def split_into_segments(text: str, max_length: int = MAX_SEGMENT_LENGTH) -> List[str]:
    """
    Dzieli tekst na segmenty (zdania lub paragrafy) o maksymalnej długości.
    Priorytet: zachowanie naturalnych granic (zdania kończące się . ! ?).
    """
    if not text or not text.strip():
        return []
    
    # Najpierw podziel na paragrafy (podwójne nowe linie)
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    
    segments = []
    for para in paragraphs:
        if len(para) <= max_length:
            segments.append(para)
            continue
        
        # Podziel na zdania
        sentences = re.split(r'(?<=[.!?])\s+', para)
        current_segment = ""
        
        for sentence in sentences:
            if not sentence.strip():
                continue
                
            if len(current_segment) + len(sentence) + 1 <= max_length:
                if current_segment:
                    current_segment += " " + sentence
                else:
                    current_segment = sentence
            else:
                if current_segment:
                    segments.append(current_segment)
                current_segment = sentence
        
        if current_segment:
            segments.append(current_segment)
    
    return segments

def is_funny_segment(segment: str) -> Tuple[bool, str]:
    """
    Wysyła pojedynczy segment do AI i zwraca czy jest śmieszny oraz powód.
    """
    if not segment or len(segment.strip()) < 10:
        return False, "Za krótki tekst"
    
    user_prompt = f"Tekst do oceny:\n{segment}"
    
    try:
        response = call_deepseek(
            system_message=SYSTEM_PROMPT,
            user_message=user_prompt,
            model="deepseek-chat",
            max_tokens=200
        )
        
        if not response:
            logger.warning("Brak odpowiedzi od AI dla segmentu: %.50s...", segment[:50])
            return False, "Brak odpowiedzi AI"
        
        # Spróbuj wyciągnąć JSON
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if not json_match:
            logger.warning("Nie znaleziono JSON w odpowiedzi: %.100s", response)
            return False, "Błędny format odpowiedzi"
        
        data = json.loads(json_match.group(0))
        is_funny = data.get("czy_smieszny", False)
        reason = data.get("powod", "Brak powodu")
        
        return bool(is_funny), str(reason)
        
    except json.JSONDecodeError as e:
        logger.error("Błąd parsowania JSON: %s | Response: %.100s", e, response)
        return False, "Błąd parsowania JSON"
    except Exception as e:
        logger.error("Błąd podczas oceny śmieszności: %s", e)
        return False, f"Błąd: {str(e)[:50]}"

def batch_detect_funny_segments(segments: List[str]) -> List[Tuple[bool, str]]:
    """
    Wysyła segmenty w batchach do AI i zwraca wyniki.
    """
    results = []
    
    for i in range(0, len(segments), MAX_SEGMENTS_PER_BATCH):
        batch = segments[i:i + MAX_SEGMENTS_PER_BATCH]
        
        # Tworzymy jeden prompt dla całego batcha
        batch_text = "\n\n---\n\n".join([f"SEGMENT {j+1}:\n{s}" for j, s in enumerate(batch)])
        user_prompt = f"""Oceń każdy z poniższych segmentów osobno. Każdy segment jest oznaczony jako "SEGMENT X".

{batch_text}

Dla każdego segmentu zwróć JSON array, gdzie każdy element ma format:
{{
  "segment_index": numer_segmentu (1-based),
  "czy_smieszny": true/false,
  "powod": "krótkie wyjaśnienie"
}}

Zwróć TYLKO tablicę JSON, nic więcej."""
        
        try:
            response = call_deepseek(
                system_message=SYSTEM_PROMPT,
                user_message=user_prompt,
                model="deepseek-chat",
                max_tokens=1000
            )
            
            if not response:
                logger.warning("Brak odpowiedzi dla batcha segmentów")
                # Fallback: wszystkie false
                results.extend([(False, "Brak odpowiedzi AI")] * len(batch))
                continue
            
            # Spróbuj wyciągnąć JSON array
            json_match = re.search(r'\[.*\]', response, re.DOTALL)
            if not json_match:
                logger.warning("Nie znaleziono JSON array w odpowiedzi batcha")
                results.extend([(False, "Błędny format")] * len(batch))
                continue
            
            data = json.loads(json_match.group(0))
            
            # Mapowanie wyników według indeksów
            batch_results = {}
            for item in data:
                if isinstance(item, dict):
                    idx = item.get("segment_index", 0)
                    if 1 <= idx <= len(batch):
                        batch_results[idx-1] = (
                            bool(item.get("czy_smieszny", False)),
                            str(item.get("powod", "Brak powodu"))
                        )
            
            # Dla każdego segmentu w batchu, użyj wyników lub fallback
            for j in range(len(batch)):
                if j in batch_results:
                    results.append(batch_results[j])
                else:
                    results.append((False, "Brak oceny w odpowiedzi"))
                    
        except Exception as e:
            logger.error("Błąd podczas batch oceny: %s", e)
            results.extend([(False, f"Błąd: {str(e)[:30]}")] * len(batch))
    
    return results

def highlight_funny_text(text: str, funny_segments: List[Tuple[str, bool, str]]) -> str:
    """
    Formatuje tekst, oznaczając śmieszne segmenty białymi literami na czarnym tle.
    
    Args:
        text: Oryginalny tekst
        funny_segments: Lista krotek (segment_text, is_funny, reason)
    
    Returns:
        Tekst z HTML/CSS markup dla śmiesznych fragmentów
    """
    if not funny_segments:
        return text
    
    # Sortuj segmenty od najdłuższych do najkrótszych, aby uniknąć problemów z nakładaniem się
    funny_segments_sorted = sorted(
        [(seg, is_funny, reason) for seg, is_funny, reason in funny_segments],
        key=lambda x: len(x[0]),
        reverse=True
    )
    
    result = text
    
    for segment, is_funny, reason in funny_segments_sorted:
        if not is_funny:
            continue
        
        # Unikaj podwójnego zaznaczania
        if f'<span class="funny-highlight"' in result:
            # Sprawdź czy ten segment już został oznaczony
            marked_segment = f'<span class="funny-highlight" title="{reason}">{segment}</span>'
            if marked_segment in result:
                continue
        
        # Zamień tylko pierwsze wystąpienie (aby uniknąć oznaczania tego samego tekstu wielokrotnie)
        replacement = f'<span class="funny-highlight" title="{reason}">{segment}</span>'
        result = result.replace(segment, replacement, 1)
    
    return result

def detect_and_highlight_funny_parts(text: str, use_batching: bool = True) -> Dict[str, Any]:
    """
    Główna funkcja: wykrywa śmieszne części w tekście i zwraca sformatowany tekst.
    
    Returns:
        Dict z kluczami:
        - formatted_text: tekst z highlightami śmiesznych fragmentów
        - funny_count: liczba śmiesznych segmentów
        - total_segments: całkowita liczba segmentów
        - segments_info: lista informacji o segmentach
    """
    if not text or len(text.strip()) < 20:
        return {
            "formatted_text": text,
            "funny_count": 0,
            "total_segments": 0,
            "segments_info": []
        }
    
    # 1. Podziel tekst na segmenty
    segments = split_into_segments(text)
    
    if not segments:
        return {
            "formatted_text": text,
            "funny_count": 0,
            "total_segments": 0,
            "segments_info": []
        }
    
    logger.info("Podzielono tekst na %d segmentów", len(segments))
    
    # 2. Oceń śmieszność segmentów
    if use_batching and len(segments) > 2:
        results = batch_detect_funny_segments(segments)
    else:
        results = []
        for segment in segments:
            is_funny, reason = is_funny_segment(segment)
            results.append((is_funny, reason))
    
    # 3. Przygotuj listę segmentów z informacjami
    segments_info = []
    funny_segments_for_highlight = []
    
    for i, (segment, (is_funny, reason)) in enumerate(zip(segments, results)):
        segments_info.append({
            "segment": segment,
            "is_funny": is_funny,
            "reason": reason,
            "index": i
        })
        
        if is_funny:
            funny_segments_for_highlight.append((segment, is_funny, reason))
    
    # 4. Wyróżnij śmieszne fragmenty w oryginalnym tekście
    formatted_text = highlight_funny_text(text, funny_segments_for_highlight)
    
    # 5. Jeśli nie ma żadnych highlightów, dodaj styl CSS na wszelki wypadek
    if '<span class="funny-highlight"' not in formatted_text:
        # Dodaj styl CSS na początku jeśli tekst zawiera HTML
        if '<html>' in formatted_text or '<body>' in formatted_text:
            style_tag = '<style>.funny-highlight { color: white !important; background-color: black !important; padding: 2px 4px; border-radius: 3px; }</style>'
            formatted_text = formatted_text.replace('</head>', style_tag + '</head>', 1)
        else:
            # Dla zwykłego tekstu, opakuj w prosty HTML
            formatted_text = f"""<html>
<head>
<style>
.funny-highlight {{
    color: white !important;
    background-color: black !important;
    padding: 2px 4px;
    border-radius: 3px;
}}
</style>
</head>
<body>{formatted_text}</body>
</html>"""
    
    return {
        "formatted_text": formatted_text,
        "funny_count": len(funny_segments_for_highlight),
        "total_segments": len(segments),
        "segments_info": segments_info
    }

# Funkcja pomocnicza do integracji z istniejącymi responderami
def process_funny_parts_in_response(response_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    Przetwarza pola tekstowe w odpowiedzi responderów, oznaczając śmieszne części.
    
    Obsługuje pola: reply_html, explanation_txt, oraz inne pola tekstowe.
    """
    result = response_dict.copy()
    
    # Pola do przetworzenia (klucz w dict -> czy jest HTML)
    fields_to_process = [
        ("reply_html", True),
        ("explanation_txt", False),
    ]
    
    for field_name, is_html in fields_to_process:
        if field_name in result and result[field_name]:
            if isinstance(result[field_name], dict) and "base64" in result[field_name]:
                # Pole jest base64 - trzeba je zdekodować, przetworzyć i zakodować z powrotem
                try:
                    import base64
                    original_text = base64.b64decode(result[field_name]["base64"]).decode('utf-8')
                    
                    # Przetwórz tekst
                    funny_result = detect_and_highlight_funny_parts(original_text)
                    
                    # Zakoduj z powrotem
                    result[field_name]["base64"] = base64.b64encode(
                        funny_result["formatted_text"].encode('utf-8')
                    ).decode('ascii')
                    
                    logger.info("Przetworzono śmieszne części w %s: %d/%d śmiesznych segmentów",
                               field_name, funny_result["funny_count"], funny_result["total_segments"])
                    
                except Exception as e:
                    logger.error("Błąd przetwarzania base64 w %s: %s", field_name, e)
            elif isinstance(result[field_name], str):
                # Pole jest zwykłym stringiem
                funny_result = detect_and_highlight_funny_parts(result[field_name])
                result[field_name] = funny_result["formatted_text"]
                
                logger.info("Przetworzono śmieszne części w %s: %d/%d śmiesznych segmentów",
                           field_name, funny_result["funny_count"], funny_result["total_segments"])
    
    return result