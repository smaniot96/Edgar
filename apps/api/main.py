# main.py

from fastapi import FastAPI

# Create an instance of FastAPI
app = FastAPI()

@app.get("/")
def read_root():
    return {"message": "Hello from api!"}

if __name__ == "__main__":
    # Run the FastAPI app using uvicorn if this script is executed directly
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
