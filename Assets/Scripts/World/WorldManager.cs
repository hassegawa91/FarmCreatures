using FarmCreatures.World.Grid;
using FarmCreatures.World.Tiles;
using UnityEngine;
using UnityEngine.Tilemaps;

namespace FarmCreatures.World
{
    public class WorldManager : MonoBehaviour
    {
        public static WorldManager Instance { get; private set; }

        [Header("Grid")]
        [SerializeField] private float cellSize = 1f;

        [Header("Tilemaps")]
        [SerializeField] private Tilemap groundTilemap;

        [Header("Data")]
        [SerializeField] private TileDatabase tileDatabase;

        public float CellSize => cellSize;
        public Tilemap GroundTilemap => groundTilemap;
        public TileDatabase TileDatabase => tileDatabase;

        private void Awake()
        {
            if (Instance != null && Instance != this)
            {
                Destroy(gameObject);
                return;
            }

            Instance = this;
        }

        public GridPosition WorldToGrid(Vector3 worldPosition)
        {
            return GridPosition.FromWorld(worldPosition, cellSize);
        }

        public Vector3 GridToWorld(GridPosition gridPosition)
        {
            return gridPosition.ToWorld(cellSize);
        }

        public bool IsWalkable(Vector3 worldPosition)
        {
            if (groundTilemap == null || tileDatabase == null)
                return true;

            Vector3Int cell = groundTilemap.WorldToCell(worldPosition);
            TileBase currentTile = groundTilemap.GetTile(cell);

            if (currentTile == null)
                return true;

            foreach (FarmCreatures.World.Tiles.TileData tileData in tileDatabase.Tiles)
            {
                if (tileData != null && tileData.tile == currentTile)
                    return tileData.walkable;
            }

            return true;
        }
    }
}
