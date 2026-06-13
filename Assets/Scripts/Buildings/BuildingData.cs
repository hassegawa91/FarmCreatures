using UnityEngine;

namespace FarmCreatures.Buildings
{
    [CreateAssetMenu(fileName = "NewBuildingData", menuName = "FarmCreatures/Buildings/Building Data")]
    public class BuildingData : ScriptableObject
    {
        public string buildingId;
        public string displayName;
        public Sprite icon;
        public Vector2Int size = Vector2Int.one;
        [TextArea] public string description;
    }
}
