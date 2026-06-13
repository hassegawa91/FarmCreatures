using System.Collections.Generic;
using UnityEngine;

namespace FarmCreatures.World.Tiles
{
    [CreateAssetMenu(fileName = "TileDatabase", menuName = "FarmCreatures/World/Tile Database")]
    public class TileDatabase : ScriptableObject
    {
        [SerializeField] private List<TileData> tiles = new List<TileData>();

        private Dictionary<string, TileData> byId;

        public IReadOnlyList<TileData> Tiles => tiles;

        private void OnEnable()
        {
            RebuildCache();
        }

        public void RebuildCache()
        {
            byId = new Dictionary<string, TileData>();

            foreach (TileData tile in tiles)
            {
                if (tile == null || string.IsNullOrWhiteSpace(tile.tileId))
                    continue;

                byId[tile.tileId] = tile;
            }
        }

        public TileData GetById(string tileId)
        {
            if (byId == null)
                RebuildCache();

            return byId != null && byId.TryGetValue(tileId, out TileData tile) ? tile : null;
        }
    }
}
