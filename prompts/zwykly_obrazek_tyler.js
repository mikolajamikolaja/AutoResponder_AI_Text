// <STYLE_CONFIG>
{
  "model_target": "FLUX.1 [schnell]",
  "optimization_strategy": "High-impact descriptive natural language. Focus on physical textures and direct spatial instructions. Minimalist but heavy on atmospheric 'grit'.",
  
  "character_profile": "Brad Pitt as Tyler Durden from 1999. Gritty, intense, unrefined movie-still look.",

  "fincher_lighting_engine": "Cinematic chiaroscuro. Sickly green and amber underexposed tones. Heavy 35mm film grain. Deep shadows, industrial grime, flickering fluorescent light effect. 1990s film stock aesthetic.",

  "triptych": {
    "instruction": "A wide horizontal 3:1 aspect ratio image divided into 3 equal side-by-side vertical panels by thin black lines. Each panel must show a distinct camera angle.",
    "panels": [
      {
        "composition": "Cinematic medium shot, 35mm film style. Tyler Durden (Brad Pitt, 1999) stands in a dark, damp industrial basement. He is pointing his finger directly at the camera lens with a menacing, intense gaze. Gritty textures, sweat on brow, flickering fluorescent overhead light.",
        "text_rendering": "A gritty, hand-drawn comic speech bubble emerges from Tyler's side. Inside the bubble, the Polish text: '[TEXT_1]' is rendered in bold, distressed black ink. The font is high-contrast, perfectly sharp, and clearly legible. The Polish characters and diacritics are accurately depicted."
      },
      {
        "panel_2": "Extreme close-up of Tyler's face. Focus on eyes and chipped tooth. Heavy sweat and skin pores visible. Dramatic shadows.",
        "text_rendering": "A gritty, hand-drawn comic speech bubble emerges from Tyler's side. Inside the bubble, the Polish text: '[TEXT_2]' is rendered in bold, distressed black ink. The font is high-contrast, perfectly sharp, and clearly legible. The Polish characters and diacritics are accurately depicted."
      },
      {
        "panel_3": "Low angle wide shot. Tyler standing victoriously over a pile of [USER_OBJECTS]. Dark urban alleyway, steam rising from pipes.",
        "text_rendering": "A gritty, hand-drawn comic speech bubble emerges from Tyler's side. Inside the bubble, the Polish text: '[TEXT_3]' is rendered in bold, distressed black ink. The font is high-contrast, perfectly sharp, and clearly legible. The Polish characters and diacritics are accurately depicted."
      }
    ]
  },

  "schnell_technical_parameters": {
    "aspect_ratio": "3:1",
    "steps_optimized": "4-8 steps",
    "guidance_scale": "3.5",
    "style_tags": "Raw photo, gritty realism, 35mm movie still, high contrast, industrial decay, realistic textures."
  },

  "safety_and_content_filter": {
    "forbidden_words": ["walka", "walczą", "faceci", "klub"],
    "replacement_logic": "Always map to sender's context (e.g., 'Project Excel', 'The Laundry War', 'Cleaning Sessions')."
  }
}
// </STYLE_CONFIG>
