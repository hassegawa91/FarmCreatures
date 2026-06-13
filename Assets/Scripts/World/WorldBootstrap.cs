using FarmCreatures.World.Chunks;
using UnityEngine;
using UnityEngine.Tilemaps;

namespace FarmCreatures.World
{
    public class WorldBootstrap : MonoBehaviour
    {
        [Header("Auto Setup")]
        [SerializeField] private bool autoFindTilemap = true;
        [SerializeField] private bool ensureChunkManager = true;

        private void Awake()
        {
            if (autoFindTilemap)
                TryAutoFindTilemap();

            if (ensureChunkManager && GetComponent<ChunkManager>() == null)
                gameObject.AddComponent<ChunkManager>();
        }

        private void TryAutoFindTilemap()
        {
            WorldManager worldManager = GetComponent<WorldManager>();

            if (worldManager == null)
                return;

            Tilemap tilemap = FindFirstObjectByType<Tilemap>();

            if (tilemap == null)
                Debug.LogWarning("WorldBootstrap: nenhum Tilemap encontrado na cena.");
        }
    }
}
