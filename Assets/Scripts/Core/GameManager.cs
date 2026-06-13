using UnityEngine;

namespace FarmCreatures.Core
{
    public enum GameState
    {
        Booting,
        Playing,
        Paused
    }

    public class GameManager : MonoBehaviour
    {
        public static GameManager Instance { get; private set; }

        [SerializeField] private GameState currentState = GameState.Booting;

        public GameState CurrentState => currentState;

        private void Awake()
        {
            if (Instance != null && Instance != this)
            {
                Destroy(gameObject);
                return;
            }

            Instance = this;
            DontDestroyOnLoad(gameObject);
        }

        private void Start()
        {
            SetState(GameState.Playing);
        }

        public void SetState(GameState newState)
        {
            currentState = newState;
            Time.timeScale = newState == GameState.Paused ? 0f : 1f;
        }

        public void TogglePause()
        {
            SetState(currentState == GameState.Paused ? GameState.Playing : GameState.Paused);
        }
    }
}
