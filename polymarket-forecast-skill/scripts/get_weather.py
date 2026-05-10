#!/usr/bin/env python3
"""
Weather forecast script using OpenWeather API.
Usage: 
  get_weather.py <location> <YYYY-MM-DD>                           # Single day forecast
  get_weather.py compare <location1> <location2> <days>             # Multi-day comparison
  get_weather.py activity <activity> <location> <days>             # Activity recommendations
"""
import os
import sys
import json
import datetime
import urllib.request
import urllib.parse
import urllib.error


def load_api_key():
    """Load API key from config.json in the skill directory"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(os.path.dirname(script_dir), 'config.json')
    
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
            api_key = config.get('api_key', '')
            
            if not api_key or api_key == 'PASTE_YOUR_API_KEY_HERE':
                return None
            return api_key.strip()
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def fetch_json(url, params):
    """Helper: GET request returning parsed JSON"""
    qs = urllib.parse.urlencode(params)
    full_url = f"{url}?{qs}"
    try:
        with urllib.request.urlopen(full_url, timeout=10) as resp:
            if resp.getcode() == 401:
                return {"error": "invalid_api_key", "message": "Invalid OpenWeather API key."}
            elif resp.getcode() == 429:
                return {"error": "quota_exceeded", "message": "API quota exceeded. Please try again later."}
            elif resp.getcode() != 200:
                return {"error": "api_error", "message": f"API returned status {resp.getcode()}"}
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"error": "invalid_api_key", "message": "Invalid OpenWeather API key."}
        elif e.code == 429:
            return {"error": "quota_exceeded", "message": "API quota exceeded. Please try again later."}
        else:
            return {"error": "http_error", "message": f"HTTP error {e.code}: {e.reason}"}
    except urllib.error.URLError as e:
        return {"error": "network_error", "message": f"Network error: {e.reason}"}
    except Exception as e:
        return {"error": "unknown_error", "message": str(e)}


def geocode(location, api_key):
    """Convert a place name to lat/lon via OWM geocoding"""
    data = fetch_json(
        "https://api.openweathermap.org/geo/1.0/direct",
        {"q": location, "limit": 1, "appid": api_key},
    )
    if "error" in data:
        print(json.dumps(data))
        sys.exit(1)
    if not data or len(data) == 0:
        print(json.dumps({
            "error": "location_not_found",
            "message": f"Location '{location}' not found. Try being more specific (e.g., 'Paris, France' instead of 'Paris')."
        }))
        sys.exit(1)
    entry = data[0]
    return entry["lat"], entry["lon"], entry.get("name", location)


def get_current_weather(lat, lon, api_key):
    """Fetch current weather data"""
    data = fetch_json(
        "https://api.openweathermap.org/data/2.5/weather",
        {
            "lat": lat,
            "lon": lon,
            "units": "metric",
            "appid": api_key,
        },
    )
    if "error" in data:
        return data
    return data


def get_forecast(lat, lon, api_key):
    """Fetch 5-day weather forecast"""
    data = fetch_json(
        "https://api.openweathermap.org/data/2.5/forecast",
        {
            "lat": lat,
            "lon": lon,
            "units": "metric",
            "appid": api_key,
        },
    )
    if "error" in data:
        return data
    return data


def pick_day(forecast, target_date):
    """Find the forecast for a specific date from 5-day forecast"""
    target = datetime.datetime.strptime(target_date, "%Y-%m-%d").date()
    
    # Look through the forecast list for matching date
    for item in forecast.get("list", []):
        forecast_date = datetime.date.fromtimestamp(item["dt"])
        if forecast_date == target:
            return item
    
    # If no exact match found, return None (will use current weather)
    return None


def get_multi_day_forecast(forecast, num_days):
    """Get forecast for multiple days from 5-day forecast"""
    forecasts = []
    daily_summaries = {}
    
    # Group forecast items by date and find daily highs/lows
    for item in forecast.get("list", []):
        date = datetime.date.fromtimestamp(item["dt"])
        date_str = date.isoformat()
        
        if date_str not in daily_summaries:
            daily_summaries[date_str] = {
                "date": date_str,
                "date_formatted": date.strftime("%A, %B %d"),
                "temps": [],
                "conditions": [],
                "humidity_values": [],
                "wind_speeds": [],
                "precip_amounts": [],
                "precip_probs": []
            }
        
        # Collect data for daily summary
        daily_summaries[date_str]["temps"].append(item["main"]["temp"])
        daily_summaries[date_str]["conditions"].append(item["weather"][0])
        daily_summaries[date_str]["humidity_values"].append(item["main"]["humidity"])
        daily_summaries[date_str]["wind_speeds"].append(item["wind"]["speed"])
        daily_summaries[date_str]["precip_probs"].append(item.get("pop", 0))
        
        # Add precipitation if it exists
        rain = item.get("rain", {}).get("3h", 0)
        snow = item.get("snow", {}).get("3h", 0)
        daily_summaries[date_str]["precip_amounts"].append(rain + snow)
    
    # Convert to final format, taking only the requested number of days
    sorted_dates = sorted(daily_summaries.keys())[:num_days]
    
    for date_str in sorted_dates:
        day_data = daily_summaries[date_str]
        temps = day_data["temps"]
        
        # Find the most common weather condition for the day
        conditions = day_data["conditions"]
        condition_mains = [c["main"] for c in conditions]
        most_common_main = max(set(condition_mains), key=condition_mains.count)
        most_common_condition = next(c for c in conditions if c["main"] == most_common_main)
        
        forecasts.append({
            "date": day_data["date"],
            "date_formatted": day_data["date_formatted"],
            "temp_high_c": round(max(temps), 1),
            "temp_high_f": round(max(temps) * 9 / 5 + 32, 1),
            "temp_low_c": round(min(temps), 1),
            "temp_low_f": round(min(temps) * 9 / 5 + 32, 1),
            "condition": most_common_condition["description"],
            "condition_main": most_common_condition["main"],
            "humidity": round(sum(day_data["humidity_values"]) / len(day_data["humidity_values"])),
            "wind_kph": round(sum(day_data["wind_speeds"]) / len(day_data["wind_speeds"]) * 3.6, 1),
            "precip_mm": round(sum(day_data["precip_amounts"]), 1),
            "precip_probability": round(max(day_data["precip_probs"]) * 100),
            "uv_index": 0  # Not available in 5-day forecast
        })
    
    return forecasts


def analyze_comparison(location1_forecasts, location2_forecasts):
    """Analyze two location forecasts and provide recommendations"""
    recommendations = []
    
    # Compare average temperatures
    avg_temp1 = sum(d["temp_high_c"] for d in location1_forecasts if d["temp_high_c"]) / len(location1_forecasts)
    avg_temp2 = sum(d["temp_high_c"] for d in location2_forecasts if d["temp_high_c"]) / len(location2_forecasts)
    
    if avg_temp1 > avg_temp2 + 5:
        recommendations.append(f"{location1_forecasts[0]['location']} will be significantly warmer")
    elif avg_temp2 > avg_temp1 + 5:
        recommendations.append(f"{location2_forecasts[0]['location']} will be significantly warmer")
    
    # Compare rain probability
    rain_days1 = sum(1 for d in location1_forecasts if d["precip_probability"] > 50)
    rain_days2 = sum(1 for d in location2_forecasts if d["precip_probability"] > 50)
    
    if rain_days1 > rain_days2:
        recommendations.append(f"{location2_forecasts[0]['location']} has fewer rainy days expected")
    elif rain_days2 > rain_days1:
        recommendations.append(f"{location1_forecasts[0]['location']} has fewer rainy days expected")
    
    # Overall recommendation
    if not recommendations:
        recommendations.append("Both locations have similar weather patterns")
    
    return recommendations


def compare_locations(location1, location2, num_days, api_key):
    """Compare weather between two locations over multiple days"""
    try:
        # Get data for both locations
        lat1, lon1, resolved1 = geocode(location1, api_key)
        forecast1 = get_forecast(lat1, lon1, api_key)
        if "error" in forecast1:
            return forecast1
        forecasts1 = get_multi_day_forecast(forecast1, num_days)
        
        lat2, lon2, resolved2 = geocode(location2, api_key)
        forecast2 = get_forecast(lat2, lon2, api_key)
        if "error" in forecast2:
            return forecast2
        forecasts2 = get_multi_day_forecast(forecast2, num_days)
        
        # Add location names to forecast data
        for f in forecasts1:
            f["location"] = resolved1
        for f in forecasts2:
            f["location"] = resolved2
        
        # Analyze and compare
        recommendations = analyze_comparison(forecasts1, forecasts2)
        
        return {
            "comparison_type": "multi_day",
            "num_days": num_days,
            "location1": {
                "name": resolved1,
                "forecasts": forecasts1
            },
            "location2": {
                "name": resolved2,
                "forecasts": forecasts2
            },
            "recommendations": recommendations,
            "generated_at": datetime.datetime.now().isoformat()
        }
        
    except Exception as e:
        return {
            "error": "comparison_failed",
            "message": f"Failed to compare locations: {str(e)}"
        }


# Activity Analysis System
ACTIVITY_PROFILES = {
    "skiing": {
        "name": "Skiing",
        "ideal_temp_range": (-15, 10),  # Broader range for different skill levels and snow types
        "max_wind_kph": 40,  # Allow stronger winds for experienced skiers
        "max_precip_rain": 5,  # Light rain might be acceptable for some
        "min_precip_snow": 0,  # any snow is good
        "factors": {
            "temperature": 0.25,  # Less emphasis on exact temperature
            "wind": 0.15,
            "precipitation": 0.35,  # More emphasis on precipitation type
            "conditions": 0.25
        }
    },
    "picnic": {
        "name": "Picnic",
        "ideal_temp_range": (10, 35),  # Much broader range - people have different comfort levels
        "max_wind_kph": 30,  # Allow moderate winds
        "max_precip_rain": 2,  # Light drizzle might be manageable with cover
        "max_humidity": 85,  # Less strict humidity requirements
        "factors": {
            "temperature": 0.20,
            "wind": 0.15,
            "precipitation": 0.40,  # Rain is main concern
            "conditions": 0.25
        }
    },
    "hiking": {
        "name": "Hiking",
        "ideal_temp_range": (0, 35),  # Very broad - hikers are adaptable
        "max_wind_kph": 50,  # Hikers can handle strong winds better
        "max_precip_rain": 8,  # Moderate rain acceptable with proper gear
        "factors": {
            "temperature": 0.20,  # Hikers dress appropriately
            "wind": 0.15,
            "precipitation": 0.35,
            "conditions": 0.30  # Conditions matter more than exact temps
        }
    },
    "gardening": {
        "name": "Gardening",
        "ideal_temp_range": (0, 40),  # Very broad - many gardening activities possible
        "max_wind_kph": 35,  # Allow moderate winds
        "ideal_humidity_range": (30, 90),  # Much broader humidity tolerance
        "factors": {
            "temperature": 0.15,  # Less critical - people garden in many temperatures
            "wind": 0.15,
            "precipitation": 0.45,  # Precipitation timing most important
            "conditions": 0.25
        }
    },
    "beach": {
        "name": "Beach Day",
        "ideal_temp_range": (18, 40),  # Broader range - some prefer cooler beach days
        "max_wind_kph": 35,  # Allow moderate winds for activities like kiting
        "max_precip_rain": 1,  # Still prefer dry conditions
        "max_humidity": 85,  # More tolerant of humidity at beach
        "factors": {
            "temperature": 0.30,  # Temperature still important for beach
            "wind": 0.15,
            "precipitation": 0.35,
            "conditions": 0.20
        }
    },
    "cycling": {
        "name": "Cycling",
        "ideal_temp_range": (0, 35),  # Very broad - cyclists are hardy
        "max_wind_kph": 40,  # Allow stronger winds
        "max_precip_rain": 3,  # Light to moderate rain manageable
        "factors": {
            "temperature": 0.20,  # Cyclists generate heat and dress appropriately
            "wind": 0.30,  # Wind is major factor for cycling
            "precipitation": 0.30,
            "conditions": 0.20
        }
    },
    # Generic catch-all profiles for broader activities
    "outdoor": {
        "name": "Outdoor Activity",
        "ideal_temp_range": (5, 30),  # Moderate range for general outdoor activities
        "max_wind_kph": 35,
        "max_precip_rain": 3,
        "factors": {
            "temperature": 0.25,
            "wind": 0.20,
            "precipitation": 0.35,
            "conditions": 0.20
        }
    },
    "sports": {
        "name": "Outdoor Sports",
        "ideal_temp_range": (0, 35),  # Athletes adapt to many conditions
        "max_wind_kph": 40,
        "max_precip_rain": 2,
        "factors": {
            "temperature": 0.20,
            "wind": 0.25,
            "precipitation": 0.35,
            "conditions": 0.20
        }
    }
}


def analyze_weather_for_activity(activity_key, forecast_data):
    """Analyze weather conditions for a specific activity"""
    if activity_key not in ACTIVITY_PROFILES:
        return None
    
    profile = ACTIVITY_PROFILES[activity_key]
    scores = []
    recommendations = []
    
    for day in forecast_data:
        score = calculate_activity_score(day, profile)
        day_rec = generate_day_recommendation(day, profile, score)
        scores.append(score)
        recommendations.append(day_rec)
    
    # Overall analysis
    avg_score = sum(scores) / len(scores) if scores else 0
    best_day = max(enumerate(forecast_data), key=lambda x: scores[x[0]]) if scores else None
    
    overall_recommendation = generate_overall_recommendation(profile, scores, recommendations)
    
    return {
        "activity": profile["name"],
        "overall_score": round(avg_score, 2),
        "overall_rating": get_rating_text(avg_score),
        "best_day": {
            "date": best_day[1]["date"],
            "date_formatted": best_day[1]["date_formatted"],
            "score": round(scores[best_day[0]], 2),
            "rating": get_rating_text(scores[best_day[0]])
        } if best_day else None,
        "daily_analysis": [
            {
                "date": day["date"],
                "date_formatted": day["date_formatted"],
                "score": round(score, 2),
                "rating": get_rating_text(score),
                "recommendation": rec
            }
            for day, score, rec in zip(forecast_data, scores, recommendations)
        ],
        "overall_recommendation": overall_recommendation
    }


def calculate_activity_score(day, profile):
    """Calculate a 0-100 score for how suitable a day is for an activity"""
    temp_score = calculate_temperature_score(day["temp_high_c"], profile)
    wind_score = calculate_wind_score(day["wind_kph"], profile)
    precip_score = calculate_precipitation_score(day, profile)
    condition_score = calculate_condition_score(day["condition_main"], profile)
    
    factors = profile["factors"]
    total_score = (
        temp_score * factors["temperature"] +
        wind_score * factors["wind"] +
        precip_score * factors["precipitation"] +
        condition_score * factors["conditions"]
    ) * 100
    
    return max(0, min(100, total_score))


def calculate_temperature_score(temp, profile):
    """Score temperature suitability (0-1) with gradual transitions"""
    min_temp, max_temp = profile["ideal_temp_range"]
    ideal_center = (min_temp + max_temp) / 2
    ideal_range = max_temp - min_temp
    
    # Perfect score at center of ideal range
    if min_temp <= temp <= max_temp:
        # Gradual score within ideal range (peak at center)
        distance_from_center = abs(temp - ideal_center)
        return 1.0 - (distance_from_center / (ideal_range / 2)) * 0.2  # Max 20% penalty within range
    
    # Gradual decline outside ideal range
    if temp < min_temp:
        diff = min_temp - temp
        return max(0, 1.0 - (diff / 20))  # 20 degree gradual tolerance
    else:
        diff = temp - max_temp
        return max(0, 1.0 - (diff / 25))  # 25 degree gradual tolerance for heat


def calculate_wind_score(wind_kph, profile):
    """Score wind suitability (0-1) with gradual decline"""
    max_wind = profile.get("max_wind_kph", 50)
    
    # Gradual score even within acceptable range
    if wind_kph <= max_wind * 0.5:  # Light winds get full score
        return 1.0
    elif wind_kph <= max_wind:  # Moderate winds get reduced score
        excess = wind_kph - (max_wind * 0.5)
        penalty = (excess / (max_wind * 0.5)) * 0.3  # Up to 30% penalty
        return 1.0 - penalty
    else:
        # Gradual decline beyond max acceptable
        excess = wind_kph - max_wind
        return max(0, 0.7 - (excess / max_wind))  # Start from 70% and decline


def calculate_precipitation_score(day, profile):
    """Score precipitation suitability (0-1) with nuanced logic"""
    precip = day["precip_mm"]
    precip_prob = day["precip_probability"]
    
    # For skiing, snow is good but rain is bad
    if profile.get("min_precip_snow") is not None and day["condition_main"] == "Snow":
        return min(1.0, 0.6 + (precip / 10))  # Snow improves score gradually
    
    # For gardening, consider timing and type of precipitation
    if "gardening" in profile["name"].lower():
        if precip_prob > 70:  # High chance of rain during activity
            return 0.3 + (0.4 * (1 - precip_prob / 100))  # 30-70% range
        elif precip > 0:  # Some recent rain is beneficial
            return 0.7 + min(0.2, precip / 10)  # 70-90% range
        else:
            return 0.6  # Dry conditions OK but not optimal
    
    # For most activities, gradual penalty for precipitation
    max_rain = profile.get("max_precip_rain", 2)
    
    if precip <= max_rain * 0.5:  # Light precip
        base_score = 0.9
    elif precip <= max_rain:  # Moderate precip
        base_score = 0.7
    else:
        # Heavy rain - gradual decline
        excess = precip - max_rain
        base_score = max(0.1, 0.6 - (excess / 15))  # Gradual decline, min 10%
    
    # Additional penalty for probability
    prob_penalty = (precip_prob / 100) * 0.2  # Up to 20% penalty for probability
    return max(0.1, base_score - prob_penalty)


def calculate_condition_score(condition_main, profile):
    """Score weather condition suitability (0-1)"""
    condition_scores = {
        "Clear": 1.0,
        "Clouds": 0.8,
        "Rain": 0.3,
        "Snow": 0.9 if "skiing" in profile["name"].lower() else 0.4,
        "Thunderstorm": 0.0,
        "Drizzle": 0.5,
        "Mist": 0.6,
        "Fog": 0.4
    }
    
    return condition_scores.get(condition_main, 0.5)


def generate_day_recommendation(day, profile, score):
    """Generate a text recommendation for a specific day"""
    temp = day["temp_high_c"]
    wind = day["wind_kph"]
    precip = day["precip_mm"]
    condition = day["condition"]
    
    if score >= 80:
        return f"Excellent conditions! {temp}°C, {condition.lower()}, light winds."
    elif score >= 60:
        return f"Good conditions. {temp}°C with {condition.lower()}."
    elif score >= 40:
        return f"Fair conditions. Watch for {get_main_concern(day, profile)}."
    else:
        return f"Not recommended. {get_main_concern(day, profile)}."


def get_main_concern(day, profile):
    """Identify the main weather concern for the activity"""
    temp = day["temp_high_c"]
    wind = day["wind_kph"]
    precip = day["precip_mm"]
    
    min_temp, max_temp = profile["ideal_temp_range"]
    
    if temp < min_temp:
        return f"too cold ({temp}°C)"
    elif temp > max_temp:
        return f"too hot ({temp}°C)"
    elif precip > profile.get("max_precip_rain", 0):
        return f"rain expected ({precip}mm)"
    elif wind > profile.get("max_wind_kph", 50):
        return f"too windy ({wind} km/h)"
    else:
        return "marginal conditions"


def generate_overall_recommendation(profile, scores, daily_recs):
    """Generate overall recommendation for the activity period"""
    avg_score = sum(scores) / len(scores) if scores else 0
    activity_name = profile["name"].lower()
    
    if avg_score >= 75:
        return f"Great period for {activity_name}! Most days have excellent conditions."
    elif avg_score >= 60:
        return f"Good period for {activity_name}. Choose the better days from the forecast."
    elif avg_score >= 40:
        return f"Mixed conditions for {activity_name}. Plan around the weather."
    else:
        return f"Poor period for {activity_name}. Consider postponing or indoor alternatives."


def get_rating_text(score):
    """Convert numeric score to text rating"""
    if score >= 80:
        return "Excellent"
    elif score >= 60:
        return "Good"
    elif score >= 40:
        return "Fair"
    elif score >= 20:
        return "Poor"
    else:
        return "Not Recommended"


def analyze_activity(activity_query, location, num_days, api_key):
    """Analyze weather for activity recommendations"""
    try:
        # Parse activity from query
        activity_key = detect_activity(activity_query)
        # Note: detect_activity now always returns something (fallback to "outdoor")
        
        # Get weather data
        lat, lon, resolved = geocode(location, api_key)
        forecast = get_forecast(lat, lon, api_key)
        if "error" in forecast:
            return forecast
        
        forecast_data = get_multi_day_forecast(forecast, num_days)
        
        # Analyze for activity
        analysis = analyze_weather_for_activity(activity_key, forecast_data)
        if not analysis:
            return {
                "error": "analysis_failed",
                "message": "Could not analyze weather for this activity"
            }
        
        analysis.update({
            "location": resolved,
            "analysis_type": "activity_recommendation",
            "query": activity_query,
            "detected_activity": activity_key,  # Show what activity was detected
            "num_days": num_days,
            "generated_at": datetime.datetime.now().isoformat()
        })
        
        return analysis
        
    except Exception as e:
        return {
            "error": "activity_analysis_failed",
            "message": f"Failed to analyze activity: {str(e)}"
        }


def detect_activity(query):
    """Detect activity type from user query with flexible matching"""
    query_lower = query.lower()
    
    activity_keywords = {
        "skiing": ["ski", "skiing", "slopes", "powder", "snowboard", "snowboarding", "alpine", "nordic"],
        "picnic": ["picnic", "outdoor lunch", "park meal", "bbq", "barbecue", "outdoor dining"],
        "hiking": ["hike", "hiking", "trail", "trek", "trekking", "walk", "walking", "rambling", "bushwalking"],
        "gardening": ["garden", "gardening", "plant", "planting", "water garden", "yard work", "landscaping", "weeding"],
        "beach": ["beach", "swimming", "sunbathing", "seaside", "coastal", "surf", "surfing"],
        "cycling": ["cycling", "bike", "biking", "bicycle", "road bike", "mountain bike", "cycle"],
        "outdoor": ["outdoor activity", "outside", "outdoor event", "outdoor work", "outdoor fun"],
        "sports": ["sport", "sports", "game", "match", "tournament", "athletic", "exercise", "workout", "training"]
    }
    
    # First pass: exact activity matches
    for activity, keywords in activity_keywords.items():
        if any(keyword in query_lower for keyword in keywords):
            return activity
    
    # Second pass: fuzzy matches for common terms
    generic_mappings = {
        "run": "sports",
        "running": "sports", 
        "jog": "sports",
        "jogging": "sports",
        "play": "sports",
        "playing": "outdoor",
        "event": "outdoor",
        "party": "outdoor",
        "festival": "outdoor",
        "concert": "outdoor",
        "photography": "outdoor",
        "fishing": "outdoor",
        "camping": "outdoor",
        "climbing": "hiking"
    }
    
    for term, activity in generic_mappings.items():
        if term in query_lower:
            return activity
    
    # Default fallback for unrecognized activities
    return "outdoor"  # Instead of None, provide a generic recommendation


def main():
    """Main entry point supporting multiple modes: current, forecast, compare, activity"""
    # Load API key from config.json
    api_key = load_api_key()
    if not api_key:
        print(json.dumps({
            "error": "missing_api_key",
            "message": "API key not configured. Please edit config.json and add your OpenWeather API key."
        }))
        sys.exit(1)

    # Parse command line arguments
    if len(sys.argv) < 2:
        print(json.dumps({
            "error": "invalid_usage",
            "message": "Usage: get_weather.py <mode> [args...]\nModes: current, forecast, compare, activity"
        }))
        sys.exit(1)

    mode = sys.argv[1].lower()
    
    try:
        if mode == "current":
            if len(sys.argv) < 3:
                raise ValueError("Usage: get_weather.py current <location>")
            location = sys.argv[2]
            result = get_current_weather_detailed(location, api_key)
            
        elif mode == "forecast":
            if len(sys.argv) < 3:
                raise ValueError("Usage: get_weather.py forecast <location> [days]")
            location = sys.argv[2]
            days = int(sys.argv[3]) if len(sys.argv) > 3 else 5
            result = get_weather_forecast_detailed(location, days, api_key)
            
        elif mode == "compare":
            if len(sys.argv) < 4:
                raise ValueError("Usage: get_weather.py compare <location1> <location2> [days]")
            location1 = sys.argv[2]
            location2 = sys.argv[3]
            days = int(sys.argv[4]) if len(sys.argv) > 4 else 5
            result = compare_locations(location1, location2, days, api_key)
            
        elif mode == "activity":
            if len(sys.argv) < 4:
                raise ValueError("Usage: get_weather.py activity <activity_type> <location> [days]")
            activity_query = sys.argv[2]
            location = sys.argv[3]
            days = int(sys.argv[4]) if len(sys.argv) > 4 else 5
            result = analyze_activity(activity_query, location, days, api_key)
            
        else:
            # Legacy support for old date-based format: location YYYY-MM-DD
            if len(sys.argv) >= 3:
                try:
                    location = sys.argv[1]
                    target_date = sys.argv[2]
                    datetime.datetime.strptime(target_date, "%Y-%m-%d")
                    result = get_legacy_weather(location, target_date, api_key)
                except ValueError:
                    result = {
                        "error": "invalid_mode",
                        "message": f"Unknown mode '{mode}'. Use: current, forecast, compare, activity"
                    }
            else:
                result = {
                    "error": "invalid_mode", 
                    "message": f"Unknown mode '{mode}'. Use: current, forecast, compare, activity"
                }
        
        print(json.dumps(result))
        
    except ValueError as e:
        print(json.dumps({
            "error": "invalid_usage",
            "message": str(e)
        }))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({
            "error": "unexpected_error",
            "message": f"An error occurred: {str(e)}"
        }))
        sys.exit(1)


def get_current_weather_detailed(location, api_key):
    """Get current weather with detailed formatting"""
    try:
        lat, lon, resolved = geocode(location, api_key)
        weather_data = get_current_weather(lat, lon, api_key)
        
        if "error" in weather_data:
            return weather_data
        
        temp_c = weather_data["main"]["temp"]
        temp_f = round(temp_c * 9 / 5 + 32, 1)
        
        return {
            "location": resolved,
            "analysis_type": "current_weather",
            "date": datetime.date.today().isoformat(),
            "temp_c": temp_c,
            "temp_f": temp_f,
            "humidity": weather_data["main"]["humidity"],
            "wind_kph": round(weather_data["wind"]["speed"] * 3.6, 1),
            "condition": weather_data["weather"][0]["description"],
            "condition_main": weather_data["weather"][0]["main"],
            "pressure_hpa": weather_data["main"]["pressure"],
            "visibility_km": round(weather_data.get("visibility", 10000) / 1000, 1),
            "sunrise_utc": datetime.datetime.utcfromtimestamp(weather_data["sys"]["sunrise"]).isoformat(),
            "sunset_utc": datetime.datetime.utcfromtimestamp(weather_data["sys"]["sunset"]).isoformat(),
            "generated_at": datetime.datetime.now().isoformat()
        }
        
    except Exception as e:
        return {
            "error": "current_weather_failed",
            "message": f"Failed to get current weather: {str(e)}"
        }


def get_weather_forecast_detailed(location, days, api_key):
    """Get detailed weather forecast"""
    try:
        lat, lon, resolved = geocode(location, api_key)
        forecast = get_forecast(lat, lon, api_key)
        
        if "error" in forecast:
            return forecast
        
        forecast_data = get_multi_day_forecast(forecast, days)
        
        return {
            "location": resolved,
            "analysis_type": "weather_forecast",
            "num_days": days,
            "daily_forecast": forecast_data,
            "generated_at": datetime.datetime.now().isoformat()
        }
        
    except Exception as e:
        return {
            "error": "forecast_failed",
            "message": f"Failed to get forecast: {str(e)}"
        }


def get_legacy_weather(location, target_date, api_key):
    """Legacy support for old date-based format"""
    try:
        lat, lon, resolved = geocode(location, api_key)
        
        # Check if target date is today - use current weather, otherwise use forecast
        today = datetime.date.today()
        target_date_obj = datetime.datetime.strptime(target_date, "%Y-%m-%d").date()
        
        if target_date_obj == today:
            # Use current weather for today
            weather_data = get_current_weather(lat, lon, api_key)
            if "error" in weather_data:
                return weather_data
            
            temp_c = weather_data["main"]["temp"]
            temp_f = round(temp_c * 9 / 5 + 32, 1)
            
            return {
                "location": resolved,
                "date": target_date,
                "temp_c": temp_c,
                "temp_f": temp_f,
                "humidity": weather_data["main"]["humidity"],
                "wind_kph": round(weather_data["wind"]["speed"] * 3.6, 1),
                "condition": weather_data["weather"][0]["description"],
                "precip_mm": 0,
                "sunrise_utc": datetime.datetime.utcfromtimestamp(weather_data["sys"]["sunrise"]).isoformat(),
                "sunset_utc": datetime.datetime.utcfromtimestamp(weather_data["sys"]["sunset"]).isoformat(),
            }
        else:
            # Use forecast for future dates
            forecast = get_forecast(lat, lon, api_key)
            if "error" in forecast:
                return forecast
            
            day = pick_day(forecast, target_date)
            if not day:
                return {
                    "error": "date_not_available",
                    "message": f"Forecast not available for {target_date}. 5-day forecast limit."
                }
            
            temp_c = day["main"]["temp"]
            temp_f = round(temp_c * 9 / 5 + 32, 1)
            
            rain_3h = day.get("rain", {}).get("3h", 0)
            snow_3h = day.get("snow", {}).get("3h", 0)
            
            return {
                "location": resolved,
                "date": target_date,
                "temp_c": temp_c,
                "temp_f": temp_f,
                "humidity": day["main"]["humidity"],
                "wind_kph": round(day["wind"]["speed"] * 3.6, 1),
                "condition": day["weather"][0]["description"],
                "precip_mm": rain_3h + snow_3h,
                "sunrise_utc": None,
                "sunset_utc": None,
            }
            
    except Exception as e:
        return {
            "error": "legacy_weather_failed",
            "message": f"Failed to get weather: {str(e)}"
        }


if __name__ == "__main__":
    main()