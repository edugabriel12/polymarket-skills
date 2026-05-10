# Prompt Templates for Forecast Skill

## Overview
The Forecast Skill provides Claude with real-time weather data from the OpenWeatherMap API.
Claude should use this Skill when a user asks about **current or forecast weather** for a **specific location or activity**.

**Prerequisites:** This skill requires an OpenWeatherMap API key to be configured in `config.json` before upload.

## Trigger Examples

Claude should consider invoking this skill when the user asks questions like:
- "What's the weather in Boulder this weekend?"
- "Should I ski at Loveland this weekend or next?"
- "Is it going to rain in San Francisco tomorrow?"
- "What's the temperature right now in Paris?"
- "Is next week better for hiking in Moab?"

## Invocation Template

When using this Skill, extract:
- The location (city, region, or landmark)
- The date or time frame (if given)
- The activity or context (if relevant)

Then call the weather script:

```bash
python scripts/get_weather.py "<location>" "<YYYY-MM-DD>"
```

## Response Guidance

Summarize the weather clearly in natural language.

Provide actionable insight if an activity is mentioned (e.g., "Next weekend looks colder with more snow, which may be better for skiing.").

Include temperature range, precipitation, and general conditions.

**Error Response Templates:**

- **missing_api_key**: "The weather service isn't configured properly. Please make sure config.json contains a valid OpenWeatherMap API key before uploading this skill."

- **invalid_api_key**: "There's an issue with the weather service configuration. Please verify the API key in config.json is correct."

- **location_not_found**: "I couldn't find weather data for that location. Could you be more specific? For example, try 'Paris, France' instead of just 'Paris'."

- **quota_exceeded**: "The weather service has reached its daily usage limit. Please try again later."

- **General failure**: "I wasn't able to get the latest weather data right now. This could be due to network issues or the weather service being temporarily unavailable."

If the Skill fails or live data isn't available, reply gracefully:

"I wasn't able to get the latest weather, but based on seasonal averages, Loveland usually has [summary] around this time."

## Developer Notes

Uses urllib.request for fetching data.

API key is stored in environment variable OPENWEATHERMAP_API_KEY.

Expected outputs should be short JSON summaries that Claude can weave into natural replies.