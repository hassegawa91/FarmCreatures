using UnityEngine;

namespace FarmCreatures.Buildings
{
    public class Building : MonoBehaviour
    {
        [SerializeField] private BuildingData data;

        public BuildingData Data => data;
    }
}
