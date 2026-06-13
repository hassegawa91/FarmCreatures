using System.IO;
using UnityEngine;

namespace FarmCreatures.Save
{
    public class SaveManager : MonoBehaviour
    {
        public static SaveManager Instance { get; private set; }

        private string SavePath => Path.Combine(Application.persistentDataPath, "save.json");

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

        public void SaveGame(SaveData data)
        {
            string json = JsonUtility.ToJson(data, true);
            File.WriteAllText(SavePath, json);
            Debug.Log($"Game saved: {SavePath}");
        }

        public SaveData LoadGame()
        {
            if (!File.Exists(SavePath))
                return new SaveData();

            string json = File.ReadAllText(SavePath);
            return JsonUtility.FromJson<SaveData>(json);
        }
    }
}
