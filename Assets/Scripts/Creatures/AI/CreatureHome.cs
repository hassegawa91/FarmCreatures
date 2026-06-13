using UnityEngine;

namespace FarmCreatures.Creatures.AI
{
    public class CreatureHome : MonoBehaviour
    {
        [SerializeField] private Vector2 homePosition;
        [SerializeField] private float allowedRadius = 6f;

        public Vector2 HomePosition => homePosition;
        public float AllowedRadius => allowedRadius;

        private void Awake()
        {
            homePosition = transform.position;
        }

        public bool IsInsideHomeArea(Vector2 position)
        {
            return Vector2.Distance(homePosition, position) <= allowedRadius;
        }

        private void OnDrawGizmosSelected()
        {
            Gizmos.color = Color.green;
            Vector3 center = Application.isPlaying ? homePosition : (Vector2)transform.position;
            Gizmos.DrawWireSphere(center, allowedRadius);
        }
    }
}
