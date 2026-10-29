Smart Energy Consumption Prediction and Control Platform
=========================================================
Follow the "Smart Energy Project - Completion Guide" step by step.

ml/Smart_Energy_ML_Model.ipynb     Step 1: clean data + train model (Google Colab)
backend/app.py                     Step 2: laptop backend (python app.py -> http://localhost:5000)
backend/simulator.py               Step 2: test without hardware (python simulator.py --backfill 4)
backend/energy_model.pkl           Trained model (replace with the one you download from Colab)
esp32/esp32_energy_monitor/        Step 4: ESP32 code (set Wi-Fi name, password, laptop IP)
