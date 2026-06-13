using System.Collections.Generic;
using UnityEngine;

namespace FarmCreatures.World.Chunks
{
    public class ChunkManager : MonoBehaviour
    {
        [SerializeField] private int chunkSize = 16;

        private readonly Dictionary<Vector2Int, WorldChunk> chunks = new Dictionary<Vector2Int, WorldChunk>();

        public int ChunkSize => chunkSize;

        public Vector2Int WorldToChunk(Vector3 worldPosition)
        {
            int x = Mathf.FloorToInt(worldPosition.x / chunkSize);
            int y = Mathf.FloorToInt(worldPosition.y / chunkSize);
            return new Vector2Int(x, y);
        }

        public WorldChunk GetOrCreateChunk(Vector2Int coordinate)
        {
            if (chunks.TryGetValue(coordinate, out WorldChunk existing))
                return existing;

            GameObject chunkObject = new GameObject($"Chunk_{coordinate.x}_{coordinate.y}");
            chunkObject.transform.SetParent(transform);
            WorldChunk chunk = chunkObject.AddComponent<WorldChunk>();
            chunk.Initialize(coordinate, chunkSize);

            chunks.Add(coordinate, chunk);
            return chunk;
        }
    }
}
