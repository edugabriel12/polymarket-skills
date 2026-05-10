FORECAST SKILL FOR CLAUDE
=========================

This weather forecast skill has been pre-configured with an API key and is ready to upload to Claude.

QUICK START
===========

1. Extract this zip file to a folder
2. Upload ALL the extracted files to Claude as a skill
3. Ensure api.openweathermap.org is in your Claude environment's allowed network domains
4. Start asking weather questions!

NETWORK REQUIREMENTS
====================

This skill requires HTTPS access to api.openweathermap.org to function.
Some Claude environments may need this domain added to their allowed network list.
If you encounter connection errors, contact your administrator about network permissions.

FILES INCLUDED
==============

- SKILL.md              → Instructions for Claude
- config.json           → Pre-configured with your API key  
- skills/get_weather.py → Weather data script
- resources/            → Additional templates and docs
- SETUP.txt             → This file

USAGE EXAMPLES
==============

Once uploaded to Claude, ask questions like:
- "Using the forecast-skill, what's the weather in Tokyo tomorrow?"
- "With forecast-skill, will it rain in London this weekend?"
- "Using forecast-skill, should I bring a jacket to Seattle next week?"
- "Using the forecast-skill, compare the weather in Paris vs London next week"
- "With forecast-skill, which city has better weather this week, Berlin or Amsterdam?"
- "Using forecast-skill, Paris or Rome for a 5-day trip?"

TIP: Starting your question with "Using the forecast-skill," or "With forecast-skill," 
ensures Claude uses real-time weather data instead of general knowledge!

TROUBLESHOOTING
===============

If the skill doesn't work:

1. Check that you uploaded ALL files (not just individual ones)
2. Verify the skill structure in Claude matches what you extracted
3. Try asking a simple weather question like "What's the weather in New York?"

The API key is already configured, so no additional setup is needed.

SECURITY NOTE
=============

This bundle contains a pre-configured API key. Keep it secure and don't share 
it publicly. If you need to share the skill, rebuild it with a fresh API key.

NEED HELP?
==========

- OpenWeather API: https://openweathermap.org/api
- Skill source code: https://github.com/kevinl95/ForecastSkill