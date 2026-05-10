# ForecastSkill

<div align="center">
  <img src="logo.png" alt="ForecastSkill Logo" width="200"/>
</div>

A Claude Skill that provides real-time weather forecasts using the OpenWeather API.

## ⚠️ Network Requirements

**Important**: This skill requires HTTPS network access to `api.openweathermap.org`. 

Some Claude environments may need this domain added to their allowed network list. If you encounter connection errors, you may need to:
- Update your Claude environment's network settings to allow `api.openweathermap.org`
- Contact your administrator if you're in a restricted environment

## Quick Start

1. Clone this repository
2. Get a free API key from [OpenWeather](https://openweathermap.org/api)
3. Run the build script and enter your API key when prompted:
   - **Linux/macOS**: `./build.sh`
   - **Windows**: `.\build.ps1`
4. Upload the generated zip to Claude.

### Build Script Options

**Bash (Linux/macOS):**
```bash
./build.sh                          # Interactive: prompts for API key
./build.sh your_api_key_here        # Non-interactive: uses provided key
./build.sh your_key custom-name.zip # Custom output filename
```

**PowerShell (Windows):**
```powershell
.\build.ps1                               # Interactive: prompts for API key
.\build.ps1 -ApiKey your_api_key_here     # Non-interactive: uses provided key
.\build.ps1 -ApiKey your_key -OutputName custom-name.zip # Custom output filename
```

Both build scripts will:
- Inject your API key into config.json
- Create a zip with files at the top level (no subfolders)
- Validate the bundle structure
- Ready for immediate upload to Claude

## Usage

Ask Claude weather questions with the skill name to activate it:

**Single Location Weather:**
- "Using the forecast-skill, what's the weather like in Paris tomorrow?"
- "With forecast-skill, will it rain in Seattle this weekend?"
- "Using forecast-skill, should I bring a jacket to Denver next week?"

**Multi-Location Comparison:** 
- "Using forecast-skill, compare weather in Paris vs London next week"
- "With the forecast-skill, which city has better weather this week, Berlin or Amsterdam?"
- "Using forecast-skill, Paris or Rome for a 5-day trip?"

**Activity Recommendations:**
- "Using forecast-skill, is it good weather for skiing in Colorado this week?"
- "With forecast-skill, when should I plan a picnic in Central Park?"
- "Using forecast-skill, what are the best days for hiking in Portland?"
- "With the forecast-skill, should I water my garden tomorrow?"

Supported activities: skiing, picnic, hiking, gardening, beach, cycling, plus many other outdoor activities

> ** Usage Tip**: For best results, start your weather questions with "Using the forecast-skill," or "With forecast-skill," to ensure Claude uses real-time weather data instead of general knowledge.

### Skill Activation

To reliably activate the forecast-skill, use one of these patterns:

**Recommended phrases:**
- "Using the forecast-skill, [your weather question]"
- "With forecast-skill, [your weather question]"
- "Can you use forecast-skill to [your weather question]"

### Local Testing

**Current Weather:**
```bash
cd forecast_skill
python skills/get_weather.py current "London"
```

**Weather Forecast:**
```bash
python skills/get_weather.py forecast "London" 5
```

**Compare Locations:**
```bash
python skills/get_weather.py compare "Paris" "London" 7
```

**Activity Recommendations:**
```bash
python skills/get_weather.py activity skiing "Denver, Colorado" 5
```

**Legacy Format (still supported):**
```bash
python skills/get_weather.py "London" "2025-11-12"
```

## Troubleshooting

**"missing_api_key" error**: 
- Edit `config.json` and add your OpenWeather API key
- Make sure you replaced the placeholder text

**"invalid_api_key" error**: 
- Verify your API key at https://openweathermap.org/api
- Check for extra spaces or quotes in config.json

**"location_not_found" error**: 
- Try a more specific location name
- Use format like "Paris, France" instead of just "Paris"

**"unknown_activity" error**:
- The system now supports many outdoor activities beyond the core list
- If an activity isn't recognized, it will default to general outdoor activity analysis
- Try using more descriptive terms if you get unexpected results

**Network domain restrictions**:
- Claude environments may need `api.openweathermap.org` added to allowed network domains
- If you get network/connection errors, check your Claude environment's network settings
- This skill requires external API access to function properly

## Features

### Weather Analysis Modes

- **Current Weather**: Real-time conditions for any location
- **Multi-Day Forecast**: Up to 7-day detailed weather forecasts
- **Location Comparison**: Side-by-side weather analysis for travel planning
- **Activity Recommendations**: Smart suggestions for outdoor activities based on weather conditions

### Activity Intelligence

The skill analyzes weather conditions for specific activities and provides:
- **Suitability Scores**: 0-100 rating for each day
- **Best Day Recommendations**: Optimal timing for your activity
- **Weather Concerns**: Specific warnings about temperature, wind, or precipitation
- **Overall Period Assessment**: Summary advice for multi-day planning

Each activity has tailored criteria:
- **Skiing**: Optimized for snow conditions, cold temperatures, manageable winds
- **Picnics**: Focuses on comfortable temperatures, dry conditions, light winds  
- **Hiking**: Very adaptable to various conditions, emphasizes safety in severe weather
- **Gardening**: Considers precipitation timing, humidity, and plant-friendly conditions
- **Beach**: Emphasizes warmth, sunshine, and minimal precipitation
- **Cycling**: Balances temperature comfort, wind resistance, and road safety
- **General Outdoor**: Flexible criteria for activities like photography, camping, festivals
- **Sports**: Adaptable profiles for running, games, and athletic activities

The system automatically adapts to recognize many outdoor activities and provides appropriate weather analysis even for activities not specifically programmed.