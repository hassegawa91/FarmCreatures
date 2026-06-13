using FarmCreatures.World.Grid;
using UnityEngine;
using UnityEngine.Tilemaps;

namespace FarmCreatures.World.Tiles
{
    [CreateAssetMenu(fileName = "NewTileData", menuName = "FarmCreatures/World/Tile Data")]
    public class TileData : ScriptableObject
    {
        [Header("Identity")]
        public string tileId;
        public string displayName;

        [Header("Visual")]
        public TileBase tile;
        public Sprite icon;

        [Header("Rules")]
        public TileType tileType = TileType.Grass;
        public bool walkable = true;
        public bool buildable = true;
        public bool farmable = false;
    }
}
