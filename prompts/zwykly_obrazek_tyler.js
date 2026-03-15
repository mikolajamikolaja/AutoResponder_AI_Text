/**
 * zwykly_obrazek_tyler.js
 * Wytyczne stylu wizualnego dla tryptyku Tylera Durdena.
 * Czytane przez zwykly.py przy generowaniu promptów FLUX.
 *
 * Format: moduł CommonJS / plain object — Python czyta go przez json.loads()
 * po wyekstrahowaniu pola STYLE_CONFIG.
 * Eksportujemy obiekt jako JEDEN blok JSON umieszczony w znaczniku
 * // <STYLE_CONFIG> ... // </STYLE_CONFIG>
 * dzięki czemu Python może go wyciąć regexpem bez parsowania całego JS.
 */

// <STYLE_CONFIG>
{
  "actor": "Brad Pitt",
  "character": "Tyler Durden",
  "film": "Fight Club (1999)",
  "director": "David Fincher",

  "base_style": "cinematic film still, 35mm grain, high contrast chiaroscuro lighting, desaturated palette with occasional warm amber highlights, Fight Club movie aesthetic, David Fincher visual style, neo-noir underground atmosphere, gritty urban decay, soap factory warehouse setting, IMAX film frame",

  "triptych": {
    "description": "Three separate vertical panels in comic-book / film storyboard layout, each 512x768px. Same visual style across all panels. Each panel has a hand-drawn speech bubble (bold white outline, off-white fill) in the upper portion where Tyler speaks.",
    "panel_width": 512,
    "panel_height": 768,
    "panels": [
      {
        "index": 1,
        "name": "zasada",
        "layout": "Brad Pitt as Tyler Durden stands in aggressive pose in a dimly lit basement, bare-chested with soap burns, pointing finger at viewer, speech bubble with one of the 8 Fight Club rules adapted to the letter-writer's situation",
        "content_source": "jedna z ośmiu zasad Tylera — losowa — dostosowana do spraw nadawcy",
        "mood": "confrontational, raw energy, underground fight club basement"
      },
      {
        "index": 2,
        "name": "manifest",
        "layout": "Brad Pitt as Tyler Durden stands on rubble of a collapsed consumer society, surrounded by burning IKEA furniture and credit cards, speech bubble with one of the 5 manifestos adapted to the letter-writer",
        "content_source": "jeden z pięciu manifestów Tylera — losowy — dostosowany do spraw nadawcy",
        "mood": "nihilistic, prophetic, anarchic liberation"
      },
      {
        "index": 3,
        "name": "chaos",
        "layout": "Brad Pitt as Tyler Durden stands next to a dumpster throwing away objects that represent the concerns mentioned in the letter, speech bubble commenting on what he is throwing away",
        "content_source": "rzeczy i tematy poruszane przez nadawcę — Tyler je wyrzuca do śmietnika",
        "mood": "liberating, darkly humorous, cathartic destruction"
      }
    ]
  },

  "speech_bubble_style": "hand-drawn comic speech bubble, thick black outline 3px, off-white fill #F5F0E8, bold black text inside, slightly tilted for dynamic feel, tail pointing toward Tyler's mouth",

  "negative_prompt": "anime, cartoon, illustration, painting, watercolor, 3d render, CGI, extra fingers, deformed hands, blurry, low quality, ugly, modern smartphone, logos",

  "quality_tags": "masterpiece, best quality, photorealistic, cinematic composition, professional cinematography, shallow depth of field, movie still"
}
// </STYLE_CONFIG>
