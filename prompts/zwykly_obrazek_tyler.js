// <STYLE_CONFIG>
{
  "model_target": "FLUX.1 [schnell]",
  "optimization_strategy": "High-impact descriptive natural language. Focus on physical textures and direct spatial instructions. Minimalist but heavy on atmospheric 'grit'.",
  
  "character_profile": "Brad Pitt as Tyler Durden from 1999. Unwashed, disheveled. Split lip, bruised cheekbone, dried blood under nose. Greasy matted hair. Shirtless with soap-burn scars and fresh bruises, or wearing a filthy torn shirt. Cigarette ash on fingers. Dark circles under bloodshot eyes. Looks like he has not slept in 3 days. Absolutely NOT clean, NOT groomed, NOT handsome. Raw, damaged, real.",

  "fincher_lighting_engine": "Cinematic chiaroscuro. Sickly green and amber underexposed tones. Heavy 35mm film grain. Deep shadows, industrial grime, flickering fluorescent light effect. 1990s film stock aesthetic.",

  "triptych": {
    "instruction": "A wide horizontal 3:1 aspect ratio image divided into 3 equal side-by-side vertical panels by thin black lines. Each panel must show a distinct camera angle.",
    "panels": [
      {
        "panel_1": "Medium shot. Tyler Durden in a dark basement. He is pointing his finger at the camera. Intimidating gaze. Industrial background.",
        "text_rendering": "A speech bubble with hand-drawn ink style containing the words: '[TEXT_1]'. Text must be sharp and legible."
      },
      {
        "panel_2": "Free composition — no layout imposed. Visual interpretation of the quote: [TEXT_2]",
        "text_rendering": "The quote '[TEXT_2]' is rendered somewhere in the scene — as graffiti, a speech bubble, or text burned into a surface. Placement and style decided by the image."
      },
      {
        "panel_3": "Low angle wide shot. Tyler standing victoriously over a pile of [USER_OBJECTS]. Dark urban alleyway, steam rising from pipes.",
        "text_rendering": "Text '[TEXT_3]' rendered as bold graffiti on the wall behind him."
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
