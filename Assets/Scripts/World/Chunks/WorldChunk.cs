using UnityEngine;

namespace FarmCreatures.World.Chunks
{
    public class WorldChunk : MonoBehaviour
    {
        [SerializeField] private Vector2Int chunkCoordinate;
        [SerializeField] private int size = 16;

        public Vector2Int ChunkCoordinate => chunkCoordinate;
        public int Size => size;

        public void Initialize(Vector2Int coordinate, int chunkSize)
        {
            chunkCoordinate = coordinate;
            size = chunkSize;
            name = $"Chunk_{coordinate.x}_{coordinate.y}";
        }
    }
}
