# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import joblib
import pandas as pd
import requests
import datetime
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split, GridSearchCV
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import mean_squared_error, r2_score
import numpy as np
from contextlib import asynccontextmanager

# Global variables to hold model and data
model = None
scaler = StandardScaler()
data = pd.DataFrame()

# Define request models
class PlayerRequest(BaseModel):
    player_name: str

# --- Lifespan handler to replace @app.on_event("startup") ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    global model, scaler, data
    try:
        model = joblib.load("fantasy_edge_rf_model.pkl")
        data = pd.read_csv('Player_Data.csv')
        features = get_features()
        X = data[features]
        scaler.fit(X)
    except Exception as e:
        print(f"Initialization error: {e}. Please retrain the model first.")
    yield  # App runs here
    # Optional shutdown logic

app = FastAPI(lifespan=lifespan)

# --- Add root endpoint to prevent 404 ---
@app.get("/")
async def root():
    return {"message": "Welcome to FantasyEdgeAI"}

# --- Helper functions remain unchanged ---
def get_features():
    return [
        "goalsScored", "assists", "cleanSheets", "penaltiesSaved", "penaltiesMissed",
        "ownGoals", "yellowCards", "redCards", "saves", "bonus", "bonusPointsSystem",
        "dreamTeamCount", "expectedGoals", "expectedAssists", "expectedGoalInvolvements",
        "expectedGoalsConceded", "expectedGoalsPer90", "expectedAssistsPer90",
        "goalsConcededPer90", "startsPer90", "cleanSheetsPer90",
        "avgPointsLast3", "maxPointsLast5", "daysSinceLastGame"
    ]

def fetch_data(url):
    try:
        response = requests.get(url)
        response.raise_for_status()
        return pd.DataFrame(response.json())
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Data fetch error: {e}")

def preprocess_data(df):
    df["playerName"] = df["firstName"] + " " + df["secondName"]
    df = df.sort_values(by=["playerName", "gameWeek"])
    
    # Create features
    df["previousPoints"] = df.groupby("playerName")["totalPoints"].shift(1)
    df["avgPointsLast3"] = df.groupby("playerName")["totalPoints"].rolling(3).mean().reset_index(0, drop=True)
    df["maxPointsLast5"] = df.groupby("playerName")["totalPoints"].rolling(5).max().reset_index(0, drop=True)
    
    # Handle datetime
    if 'gameWeek' in df.columns:
        df['gameWeek'] = pd.to_datetime(df['gameWeek'], errors='coerce')
        df['daysSinceLastGame'] = (datetime.datetime.now() - df['gameWeek']).dt.days
    
    df = df.dropna(subset=["previousPoints", "avgPointsLast3", "maxPointsLast5"])
    return df

# --- API endpoints remain unchanged except for /retrain and /predict ---
@app.post("/retrain")
async def retrain_model():
    global model, scaler, data
    
    # Fetch new data
    url = 'http://fantasyedgeai.runaspnet/api/player/data'  # Fixed URL typo (.runasp.net)
    data = fetch_data(url)
    
    # Preprocess data
    data = preprocess_data(data)
    data.to_csv('Player_Data.csv', index=False)
    
    # Prepare features/target
    features = get_features()
    X = data[features]
    y = data["totalPoints"]
    
    # Feature Scaling
    X_scaled = scaler.fit_transform(X)
    
    # Train-Test Split
    X_train, X_test, y_train, y_test = train_test_split(X_scaled, y, test_size=0.2, random_state=42)
    
    # Hyperparameter Tuning
    param_grid = {
        'n_estimators': [100, 200, 300],
        'max_depth': [5, 10, 15],
        'min_samples_split': [2, 5, 10],
        'min_samples_leaf': [1, 2, 4],
        'max_features': ['sqrt', 'log2']
    }
    
    rf_model = RandomForestRegressor(random_state=42)
    grid_search = GridSearchCV(
        estimator=rf_model,
        param_grid=param_grid,
        cv=5,
        n_jobs=-1,
        scoring='neg_mean_squared_error'
    )
    
    grid_search.fit(X_train, y_train)
    best_params = grid_search.best_params_
    
    # Train final model
    model = RandomForestRegressor(random_state=42, **best_params)
    model.fit(X_train, y_train)
    
    # Save updated model and scaler
    joblib.dump(model, "fantasy_edge_rf_model.pkl")
    joblib.dump(scaler, "scaler.pkl")
    
    return {"message": "Model retrained successfully", "best_params": best_params}

@app.post("/predict")
async def predict(player_request: PlayerRequest):
    if model is None:
        raise HTTPException(status_code=503, detail="Model not trained yet")
    
    player_name = player_request.player_name
    player_data = data[data["playerName"] == player_name]
    
    if player_data.empty:
        raise HTTPException(status_code=404, detail="Player not found")
    
    try:
        features = get_features()
        player_features = player_data[features].iloc[-1:]
        player_features_scaled = scaler.transform(player_features)
        
        predicted_points = model.predict(player_features_scaled)[0]
        previous_points = player_data["previousPoints"].iloc[-1]
        
        percentage_change = ((predicted_points - previous_points) / previous_points * 100) if previous_points != 0 else 0
        trend = "Increasing" if percentage_change > 0 else "Decreasing"
        
        # Ensure "position" column exists in data
        position = player_data["position"].values[0]

        result = {
            "playerName": player_name,
            "predictedPoints": round(float(predicted_points), 2),  # Predicted Points
            "percentageChange": f"{round(percentage_change, 2)}%",  # Percentage Change
            "trend": trend,  # Trend (Increasing/Decreasing)
            "averageBonusPoints": round(float(player_data["bonus"].tail(5).mean()), 2),  # Average Bonus Points (last 5 games)
            "pointsPerWeek": round(float(player_data["totalPoints"].tail(5).mean()), 2),  # Points Per Week (last 5 games)
        }

        if position != 1:  # Not goalkeeper
            # Calculate assists/goals as % of games with contributions
            total_games = 5  # Last 5 games
            assists_count = player_data["assists"].tail(5).sum()
            goals_count = player_data["goalsScored"].tail(5).sum()
            
            result.update({
                # Percentage of games with at least 1 assist
                "assistsPercentage": f"{(assists_count / total_games * 100):.1f}%",
                # Percentage of games with at least 1 goal
                "goalsPercentage": f"{(goals_count / total_games * 100):.1f}%"
            })
        else:
            clean_sheets = player_data["cleanSheets"].tail(5).sum()
            result["cleanSheetsLast5"] = f"{(clean_sheets / total_games * 100):.1f}%"
            
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)